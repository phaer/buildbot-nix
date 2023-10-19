#!/usr/bin/env python3

import json
import multiprocessing
import os
import signal
import sys
import uuid
from collections import defaultdict
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from buildbot.configurators import ConfiguratorBase
from buildbot.plugins import reporters, schedulers, secrets, steps, util, worker
from buildbot.process import buildstep, logobserver, remotecommand
from buildbot.process.log import Log
from buildbot.process.project import Project
from buildbot.process.properties import Interpolate, Properties
from buildbot.process.results import ALL_RESULTS, statusToString
from buildbot.steps.trigger import Trigger
from github_projects import (  # noqa: E402
    GithubProject,
#    create_project_hook,
#    load_projects,
#    refresh_projects,
)
from twisted.internet import defer, threads
from twisted.python.failure import Failure


class BuildTrigger(Trigger):
    """
    Dynamic trigger that creates a build for every attribute.
    """

    def __init__(
        self, scheduler: str, jobs: list[dict[str, Any]], **kwargs: Any
    ) -> None:
        if "name" not in kwargs:
            kwargs["name"] = "trigger"
        self.jobs = jobs
        self.config = None
        Trigger.__init__(
            self,
            waitForFinish=True,
            schedulerNames=[scheduler],
            haltOnFailure=True,
            flunkOnFailure=True,
            sourceStamps=[],
            alwaysUseLatest=False,
            updateSourceStamp=False,
            **kwargs,
        )

    def createTriggerProperties(self, props: Any) -> Any:  # noqa: N802
        return props

    def getSchedulersAndProperties(self) -> list[tuple[str, Properties]]:  # noqa: N802
        build_props = self.build.getProperties()
        repo_name = build_props.getProperty(
            "github.base.repo.full_name",
            build_props.getProperty("github.repository.full_name"),
        )
        project_id = repo_name.replace("/", "-")
        source = f"nix-eval-{project_id}"

        sch = self.schedulerNames[0]
        triggered_schedulers = []
        for job in self.jobs:
            attr = job.get("attr", "eval-error")
            name = attr
            if repo_name is not None:
                name = f"github:{repo_name}#checks.{name}"
            else:
                name = f"checks.{name}"
            drv_path = job.get("drvPath")
            error = job.get("error")
            system = job.get("system")
            out_path = job.get("outputs", {}).get("out")

            build_props.setProperty(f"{attr}-out_path", out_path, source)
            build_props.setProperty(f"{attr}-drv_path", drv_path, source)

            props = Properties()
            props.setProperty("virtual_builder_name", name, source)
            props.setProperty("status_name", f"nix-build .#checks.{attr}", source)
            props.setProperty("virtual_builder_tags", "", source)
            props.setProperty("attr", attr, source)
            props.setProperty("system", system, source)
            props.setProperty("drv_path", drv_path, source)
            props.setProperty("out_path", out_path, source)
            # we use this to identify builds when running a retry
            props.setProperty("build_uuid", str(uuid.uuid4()), source)
            props.setProperty("error", error, source)
            triggered_schedulers.append((sch, props))
        return triggered_schedulers

    def getCurrentSummary(self) -> dict[str, str]:  # noqa: N802
        """
        The original build trigger will the generic builder name `nix-build` in this case, which is not helpful
        """
        if not self.triggeredNames:
            return {"step": "running"}
        summary = []
        if self._result_list:
            for status in ALL_RESULTS:
                count = self._result_list.count(status)
                if count:
                    summary.append(
                        f"{self._result_list.count(status)} {statusToString(status, count)}"
                    )
        return {"step": f"({', '.join(summary)})"}


class NixEvalCommand(buildstep.ShellMixin, steps.BuildStep):
    """
    Parses the output of `nix-eval-jobs` and triggers a `nix-build` build for
    every attribute.
    """

    def __init__(self, supported_systems: list[str], **kwargs: Any) -> None:
        kwargs = self.setupShellMixin(kwargs)
        super().__init__(**kwargs)
        self.observer = logobserver.BufferLogObserver()
        self.addLogObserver("stdio", self.observer)
        self.supported_systems = supported_systems

    @defer.inlineCallbacks
    def run(self) -> Generator[Any, object, Any]:
        # run nix-instanstiate to generate the dict of stages
        cmd: remotecommand.RemoteCommand = yield self.makeRemoteShellCommand()
        yield self.runCommand(cmd)

        # if the command passes extract the list of stages
        result = cmd.results()
        if result == util.SUCCESS:
            # create a ShellCommand for each stage and add them to the build
            jobs = []

            for line in self.observer.getStdout().split("\n"):
                if line != "":
                    try:
                        job = json.loads(line)
                    except json.JSONDecodeError as e:
                        raise Exception(f"Failed to parse line: {line}") from e
                    jobs.append(job)
            build_props = self.build.getProperties()
            repo_name = build_props.getProperty(
                "github.base.repo.full_name",
                build_props.getProperty("github.repository.full_name"),
            )
            project_id = repo_name.replace("/", "-")
            scheduler = f"{project_id}-nix-build"
            filtered_jobs = []
            for job in jobs:
                system = job.get("system")
                if not system:  # report eval errors
                    filtered_jobs.append(job)
                elif system in self.supported_systems:
                    filtered_jobs.append(job)

            self.build.addStepsAfterCurrentStep(
                [BuildTrigger(scheduler=scheduler, name="build flake", jobs=jobs)]
            )

        return result


# FIXME this leaks memory... but probably not enough that we care
class RetryCounter:
    def __init__(self, retries: int) -> None:
        self.builds: dict[uuid.UUID, int] = defaultdict(lambda: retries)

    def retry_build(self, id: uuid.UUID) -> int:
        retries = self.builds[id]
        if retries > 1:
            self.builds[id] = retries - 1
            return retries
        else:
            return 0


# For now we limit this to two. Often this allows us to make the error log
# shorter because we won't see the logs for all previous succeeded builds
RETRY_COUNTER = RetryCounter(retries=2)


class NixBuildCommand(buildstep.ShellMixin, steps.BuildStep):
    """
    Builds a nix derivation if evaluation was successful,
    otherwise this shows the evaluation error.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs = self.setupShellMixin(kwargs)
        super().__init__(**kwargs)
        self.observer = logobserver.BufferLogObserver()
        self.addLogObserver("stdio", self.observer)

    @defer.inlineCallbacks
    def run(self) -> Generator[Any, object, Any]:
        error = self.getProperty("error")
        if error is not None:
            attr = self.getProperty("attr")
            # show eval error
            self.build.results = util.FAILURE
            log: Log = yield self.addLog("nix_error")
            log.addStderr(f"{attr} failed to evaluate:\n{error}")
            return util.FAILURE

        # run `nix build`
        cmd: remotecommand.RemoteCommand = yield self.makeRemoteShellCommand()
        yield self.runCommand(cmd)

        res = cmd.results()
        if res == util.FAILURE:
            retries = RETRY_COUNTER.retry_build(self.getProperty("build_uuid"))
            if retries > 0:
                return util.RETRY
        return res


class UpdateBuildOutput(steps.BuildStep):
    """
    Updates store paths in a public www directory.
    This is useful to prefetch updates without having to evaluate
    on the target machine.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    def run(self) -> Generator[Any, object, Any]:
        props = self.build.getProperties()
        if props.getProperty("branch") != props.getProperty(
            "github.repository.default_branch"
        ):
            return util.SKIPPED
        attr = os.path.basename(props.getProperty("attr"))
        out_path = props.getProperty("out_path")
        # XXX don't hardcode this
        p = Path("/var/www/buildbot/nix-outputs/")
        os.makedirs(p, exist_ok=True)
        (p / attr).write_text(out_path)
        return util.SUCCESS


class ReloadGithubProjects(steps.BuildStep):
    name = "reload_github_projects"

    def __init__(self, token: str, project_cache_file: Path, **kwargs: Any) -> None:
        self.token = token
        self.project_cache_file = project_cache_file
        super().__init__(**kwargs)

    def reload_projects(self) -> None:
        refresh_projects(self.token, self.project_cache_file)

    @defer.inlineCallbacks
    def run(self) -> Generator[Any, object, Any]:
        d = threads.deferToThread(self.reload_projects)

        self.error_msg = ""

        def error_cb(failure: Failure) -> int:
            self.error_msg += failure.getTraceback()
            return util.FAILURE

        d.addCallbacks(lambda _: util.SUCCESS, error_cb)
        res = yield d
        if res == util.SUCCESS:
            # reload the buildbot config
            os.kill(os.getpid(), signal.SIGHUP)
            return util.SUCCESS
        else:
            log: Log = yield self.addLog("log")
            log.addStderr(f"Failed to reload project list: {self.error_msg}")
            return util.FAILURE


def reload_github_projects(
    worker_names: list[str],
    github_token_secret: str,
    project_cache_file: Path,
) -> util.BuilderConfig:
    """
    Updates the flake an opens a PR for it.
    """
    factory = util.BuildFactory()
    factory.addStep(
        ReloadGithubProjects(github_token_secret, project_cache_file=project_cache_file)
    )
    return util.BuilderConfig(
        name="reload-github-projects",
        workernames=worker_names,
        factory=factory,
    )


def nix_update_flake_config(
    project: GithubProject,
    worker_names: list[str],
    github_token_secret: str,
    github_bot_user: str,
) -> util.BuilderConfig:
    """
    Updates the flake an opens a PR for it.
    """
    factory = util.BuildFactory()
    url_with_secret = util.Interpolate(
        f"https://git:%(secret:{github_token_secret})s@github.com/{project.name}"
    )
    factory.addStep(
        steps.Git(
            repourl=url_with_secret,
            alwaysUseLatest=True,
            method="clean",
            submodules=True,
            haltOnFailure=True,
        )
    )
    factory.addStep(
        steps.ShellCommand(
            name="Update flakes",
            env=dict(
                GIT_AUTHOR_NAME=github_bot_user,
                GIT_AUTHOR_EMAIL=f"{github_bot_user}@users.noreply.github.com",
                GIT_COMMITTER_NAME=github_bot_user,
                GIT_COMMITTER_EMAIL=f"{github_bot_user}@users.noreply.github.com",
            ),
            command=[
                "nix",
                "flake",
                "update",
                "--commit-lock-file",
                "--commit-lockfile-summary",
                "flake.lock: Update",
            ],
            haltOnFailure=True,
        )
    )
    factory.addStep(
        steps.ShellCommand(
            name="Force-Push to update_flake_lock branch",
            command=[
                "git",
                "push",
                "--force",
                "origin",
                "HEAD:refs/heads/update_flake_lock",
            ],
            haltOnFailure=True,
        )
    )
    factory.addStep(
        steps.SetPropertyFromCommand(
            env=dict(GITHUB_TOKEN=util.Secret(github_token_secret)),
            command=[
                "gh",
                "pr",
                "view",
                "--json",
                "state",
                "--template",
                "{{.state}}",
                "update_flake_lock",
            ],
            decodeRC={0: "SUCCESS", 1: "SUCCESS"},
            property="has_pr",
        )
    )
    factory.addStep(
        steps.ShellCommand(
            name="Create pull-request",
            env=dict(GITHUB_TOKEN=util.Secret(github_token_secret)),
            command=[
                "gh",
                "pr",
                "create",
                "--repo",
                project.name,
                "--title",
                "flake.lock: Update",
                "--body",
                "Automatic buildbot update",
                "--head",
                "refs/heads/update_flake_lock",
                "--base",
                project.default_branch,
            ],
            doStepIf=lambda s: s.getProperty("has_pr") != "OPEN",
        )
    )
    return util.BuilderConfig(
        name=f"{project.name}/update-flake",
        project=project.name,
        workernames=worker_names,
        factory=factory,
    )


def nix_eval_config(
    project: GithubProject,
    worker_names: list[str],
    github_token_secret: str,
    supported_systems: list[str],
    max_memory_size: int = 4096,
) -> util.BuilderConfig:
    """
    Uses nix-eval-jobs to evaluate hydraJobs from flake.nix in parallel.
    For each evaluated attribute a new build pipeline is started.
    """
    factory = util.BuildFactory()
    # check out the source
    url_with_secret = util.Interpolate(
        f"https://git:%(secret:{github_token_secret})s@github.com/%(prop:project)s"
    )
    factory.addStep(
        steps.Git(
            repourl=url_with_secret,
            method="clean",
            submodules=True,
            haltOnFailure=True,
        )
    )

    factory.addStep(
        NixEvalCommand(
            env={},
            name="evaluate flake",
            supported_systems=supported_systems,
            command=[
                "nix-eval-jobs",
                "--workers",
                multiprocessing.cpu_count(),
                "--max-memory-size",
                str(max_memory_size),
                "--option",
                "accept-flake-config",
                "true",
                "--gc-roots-dir",
                # FIXME: don't hardcode this
                "/var/lib/buildbot-worker/gcroot",
                "--force-recurse",
                "--flake",
                ".#checks",
            ],
            haltOnFailure=True,
        )
    )

    return util.BuilderConfig(
        name=f"{project.name}/nix-eval",
        workernames=worker_names,
        project=project.name,
        factory=factory,
        properties=dict(status_name="nix-eval"),
    )


def nix_build_config(
    project: GithubProject,
    worker_names: list[str],
    has_cachix_auth_token: bool = False,
    has_cachix_signing_key: bool = False,
) -> util.BuilderConfig:
    """
    Builds one nix flake attribute.
    """
    factory = util.BuildFactory()
    factory.addStep(
        NixBuildCommand(
            env={},
            name="Build flake attr",
            command=[
                "nix",
                "build",
                "-L",
                "--option",
                "keep-going",
                "true",
                "--accept-flake-config",
                "--out-link",
                util.Interpolate("result-%(prop:attr)s"),
                util.Interpolate("%(prop:drv_path)s^*"),
            ],
            haltOnFailure=True,
        )
    )
    if has_cachix_auth_token or has_cachix_signing_key:
        if has_cachix_signing_key:
            env = dict(CACHIX_SIGNING_KEY=util.Secret("cachix-signing-key"))
        else:
            env = dict(CACHIX_AUTH_TOKEN=util.Secret("cachix-auth-token"))
        factory.addStep(
            steps.ShellCommand(
                name="Upload cachix",
                env=env,
                command=[
                    "cachix",
                    "push",
                    util.Secret("cachix-name"),
                    util.Interpolate("result-%(prop:attr)s"),
                ],
            )
        )

    factory.addStep(
        steps.ShellCommand(
            name="Register gcroot",
            command=[
                "nix-store",
                "--add-root",
                # FIXME: cleanup old build attributes
                util.Interpolate(
                    "/nix/var/nix/gcroots/per-user/buildbot-worker/%(prop:project)s/%(prop:attr)s"
                ),
                "-r",
                util.Property("out_path"),
            ],
            doStepIf=lambda s: s.getProperty("branch")
            == s.getProperty("github.repository.default_branch"),
        )
    )
    factory.addStep(
        steps.ShellCommand(
            name="Delete temporary gcroots",
            command=["rm", "-f", util.Interpolate("result-%(prop:attr)s")],
        )
    )
    factory.addStep(UpdateBuildOutput(name="Update build output"))
    return util.BuilderConfig(
        name=f"{project.name}/nix-build",
        project=project.name,
        workernames=worker_names,
        collapseRequests=False,
        env={},
        factory=factory,
    )


def read_secret_file(secret_name: str) -> str:
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if directory is None:
        print("directory not set", file=sys.stderr)
        sys.exit(1)
    return Path(directory).joinpath(secret_name).read_text()


@dataclass
class ForgeConfig:
    oauth_id: str
    admins: list[str]
    buildbot_user: str
    oauth_secret_name: str
    webhook_secret_name: str
    token_secret_name: str
    project_cache_file: Path
    topic: str | None = "build-with-buildbot"

    def token(self) -> str:
        return read_secret_file(self.token_secret_name)


class GithubConfig(ForgeConfig):
    oauth_secret_name: str = "github-oauth-secret"
    webhook_secret_name: str = "github-webhook-secret"
    token_secret_name: str = "github-token"
    project_cache_file: Path = Path("github-project-cache.json")
    topic: str | None = "build-with-buildbot"


class GiteaConfig(ForgeConfig):
    root_uri: str
    oauth_secret_name: str = "gigtea-oauth-secret"
    webhook_secret_name: str = "gigtea-webhook-secret"
    token_secret_name: str = "gigtea-token"
    project_cache_file: Path = Path("gitea-project-cache.json")


def config_for_project(
    config: dict[str, Any],
    project: GithubProject,
    credentials: str,
    worker_names: list[str],
    forge: ForgeConfig,
    nix_supported_systems: list[str],
    nix_eval_max_memory_size: int,
) -> Project:
    ## get a deterministic jitter for the project
    # random.seed(project.name)
    ## don't run all projects at the same time
    # jitter = random.randint(1, 60) * 60

    config["projects"].append(Project(project.name))
    config["schedulers"].extend(
        [
            schedulers.SingleBranchScheduler(
                name=f"default-branch-{project.id}",
                change_filter=util.ChangeFilter(
                    repository=project.url,
                    filter_fn=lambda c: c.branch
                    == c.properties.getProperty("github.repository.default_branch"),
                ),
                builderNames=[f"{project.name}/nix-eval"],
            ),
            # this is compatible with bors or github's merge queue
            schedulers.SingleBranchScheduler(
                name=f"merge-queue-{project.id}",
                change_filter=util.ChangeFilter(
                    repository=project.url,
                    branch_re="(gh-readonly-queue/.*|staging|trying)",
                ),
                builderNames=[f"{project.name}/nix-eval"],
            ),
            # build all pull requests
            schedulers.SingleBranchScheduler(
                name=f"prs-{project.id}",
                change_filter=util.ChangeFilter(
                    repository=project.url, category="pull"
                ),
                builderNames=[f"{project.name}/nix-eval"],
            ),
            # this is triggered from `nix-eval`
            schedulers.Triggerable(
                name=f"{project.id}-nix-build",
                builderNames=[f"{project.name}/nix-build"],
            ),
            # allow to manually trigger a nix-build
            schedulers.ForceScheduler(
                name=f"{project.id}-force", builderNames=[f"{project.name}/nix-eval"]
            ),
            # allow to manually update flakes
            schedulers.ForceScheduler(
                name=f"{project.id}-update-flake",
                builderNames=[f"{project.name}/update-flake"],
                buttonName="Update flakes",
            ),
            # updates flakes once a week
            # schedulers.Periodic(
            #    name=f"{project.id}-update-flake-weekly",
            #    builderNames=[f"{project.name}/update-flake"],
            #    periodicBuildTimer=24 * 60 * 60 * 7 + jitter,
            # ),
        ]
    )
    has_cachix_auth_token = os.path.isfile(
        os.path.join(credentials, "cachix-auth-token")
    )
    has_cachix_signing_key = os.path.isfile(
        os.path.join(credentials, "cachix-signing-key")
    )
    config["builders"].extend(
        [
            # Since all workers run on the same machine, we only assign one of them to do the evaluation.
            # This should prevent exessive memory usage.
            nix_eval_config(
                project,
                [worker_names[0]],
                github_token_secret=github.token_secret_name,
                supported_systems=nix_supported_systems,
                max_memory_size=nix_eval_max_memory_size,
            ),
            nix_build_config(
                project,
                worker_names,
                has_cachix_auth_token,
                has_cachix_signing_key,
            ),
            nix_update_flake_config(
                project,
                worker_names,
                github_token_secret=github.token_secret_name,
                github_bot_user=github.buildbot_user,
            ),
        ]
    )


class NixConfigurator(ConfiguratorBase):
    def __init__(
        self,
        github: GithubConfig,
        gitea: GiteaConfig
        url: str,
        nix_supported_systems: list[str],
        nix_eval_max_memory_size: int = 4096,
        # Shape of this file:
        # [ { "name": "<worker-name>", "pass": "<worker-password>", "cores": "<cpu-cores>" } ]
        nix_workers_secret_name: str = "buildbot-nix-workers",
    ) -> None:
        super().__init__()
        self.nix_workers_secret_name = nix_workers_secret_name
        self.nix_eval_max_memory_size = nix_eval_max_memory_size
        self.nix_supported_systems = nix_supported_systems

        if github and gitea:
            raise Exception("We only support a single forge per buildbot instance at the moment")
        elif github:
            self.forge = github
        elif:
            self.forge = gitea
        else:
            raise Exception("No forge enabled")

        self.url = url
        self.systemd_credentials_dir = os.environ["CREDENTIALS_DIRECTORY"]


    def configure(self, config: dict[str, Any]) -> None:
        projects = load_projects(self.github.token(), self.github.project_cache_file)
        if self.github.topic is not None:
            projects = [p for p in projects if self.github.topic in p.topics]
        worker_config = json.loads(read_secret_file(self.nix_workers_secret_name))
        worker_names = []
        config["workers"] = config.get("workers", [])
        for item in worker_config:
            cores = item.get("cores", 0)
            for i in range(cores):
                worker_name = f"{item['name']}-{i}"
                config["workers"].append(worker.Worker(worker_name, item["pass"]))
                worker_names.append(worker_name)


        config["projects"] = config.get("projects", [])

        webhook_secret = read_secret_file(self.github.webhook_secret_name)

        #for project in projects:
        #    create_project_hook(
        #        project.owner,
        #        project.repo,
        #        self.github.token(),
        #        f"{self.url}/change_hook/github",
        #        webhook_secret,
        #    )

        #for project in projects:
        #    config_for_project(
        #        config,
        #        project,
        #        self.systemd_credentials_dir,
        #        worker_names,
        #        self.github,
        #        self.nix_supported_systems,
        #        self.nix_eval_max_memory_size,
        #    )

        ## Reload github projects
        #config["builders"].append(
        #    reload_github_projects(
        #        [worker_names[0]],
        #        self.github.token(),
        #        self.github.project_cache_file,
        #    )
        #)
        #config["schedulers"].extend(
        #    [
        #        schedulers.ForceScheduler(
        #            name="reload-github-projects",
        #            builderNames=["reload-github-projects"],
        #            buttonName="Update projects",
        #        ),
        #        # project list twice a day
        #        schedulers.Periodic(
        #            name="reload-github-projects-bidaily",
        #            builderNames=["reload-github-projects"],
        #            periodicBuildTimer=12 * 60 * 60,
        #        ),
        #    ]
        #)
        #config["services"] = config.get("services", [])
        #config["services"].append(
        #    reporters.GitHubStatusPush(
        #        token=self.github.token(),
        #        # Since we dynamically create build steps,
        #        # we use `virtual_builder_name` in the webinterface
        #        # so that we distinguish what has beeing build
        #        context=Interpolate("buildbot/%(prop:status_name)s"),
        #    )
        #)
        #systemd_secrets = secrets.SecretInAFile(
        #    dirname=os.environ["CREDENTIALS_DIRECTORY"]
        #)
        #config["secretsProviders"] = config.get("secretsProviders", [])
        #config["secretsProviders"].append(systemd_secrets)
        #config["www"] = config.get("www", {})
        #config["www"]["avatar_methods"] = config["www"].get("avatar_methods", [])
        #config["www"]["avatar_methods"].append(util.AvatarGitHub())
        #config["www"]["auth"] = util.GitHubAuth(
        #    self.github.oauth_id, read_secret_file(self.github.oauth_secret_name)
        #)
        #config["www"]["authz"] = util.Authz(
        #    roleMatchers=[
        #        util.RolesFromUsername(roles=["admin"], usernames=self.github.admins)
        #    ],
        #    allowRules=[
        #        util.AnyEndpointMatcher(role="admin", defaultDeny=False),
        #        util.AnyControlEndpointMatcher(role="admins"),
        #    ],
        #)
        #config["www"]["change_hook_dialects"] = config["www"].get(
        #    "change_hook_dialects", {}
        #)
        #config["www"]["change_hook_dialects"]["github"] = {
        #    "secret": webhook_secret,
        #    "strict": True,
        #    "token": self.github.token(),
        #    "github_property_whitelist": "*",
        #}
