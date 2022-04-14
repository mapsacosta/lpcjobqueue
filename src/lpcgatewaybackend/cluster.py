import asyncio
import logging
import os
import random
import shutil
import socket
import sys
import tempfile
import weakref

import dask
import yaml
from dask_jobqueue.htcondor import (
    quote_arguments,
    quote_environment,
)
from lpcjobqueue import (
    LPCCondorJob,
    LPCCondorCluster,
)   
from distributed.core import Status

import lpcjobqueue.patch  # noqa: F401

from lpcjobqueue.schedd import SCHEDD, SCHEDD_POOL, htcondor

logger = logging.getLogger(__name__)
fn = os.path.join(os.path.dirname(__file__), "config.yaml")
dask.config.ensure_file(source=fn)

with open(fn) as f:
    defaults = yaml.safe_load(f)

dask.config.update(dask.config.config, defaults, priority="old")

def is_venv():
    return hasattr(sys, "real_prefix") or (
        hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix
    )


class LPCGatewayCondorJob(LPCCondorJob):
    executable = os.path.dirname(os.path.abspath(__file__)) + '/condor_exec.exe'
    container_prefix = "/cvmfs/unpacked.cern.ch/registry.hub.docker.com/"
    config_name = "lpccondor"
    known_jobs = set()

    def __init__(
        self,
        scheduler=None,
        name=None,
        *,
        ship_env,
        image,
        **base_class_kwargs,
    ):
        image = self.container_prefix + image
        if ship_env:
            base_class_kwargs["python"] = ".env/bin/python"
            base_class_kwargs.setdefault(
                "extra", list(dask.config.get("jobqueue.%s.extra" % self.config_name))
            )
            base_class_kwargs["extra"].extend(["--preload", "lpcjobqueue.patch"])
        else:
            base_class_kwargs["python"] = "python"
        super().__init__(scheduler=scheduler, name=name, **base_class_kwargs)
        if self.log_directory:
            if not any(
                os.path.commonprefix([self.log_directory, p]) == p
                for p in LPCCondorCluster.schedd_safe_paths
            ):
                raise ValueError(
                    f"log_directory must be a subpath of one of {LPCCondorCluster.schedd_safe_paths} or else the schedd cannot write our logs back to the container"
                )

        self.job_header_dict.update(
            {
                "use_x509userproxy": "true",
                "when_to_transfer_output": "ON_EXIT_OR_EVICT",
                "transfer_output_files": "",
                "+SingularityImage": f'"{image}"',
            }
        )

    def job_script(self):
        """Construct a job submission script"""
        quoted_arguments = quote_arguments(self._command_template.split(" "))
        quoted_environment = quote_environment(self.env_dict)
        job_header_lines = "\n".join(
            "%s = %s" % (k, v) for k, v in self.job_header_dict.items()
        )
        return self._script_template % {
            "shebang": self.shebang,
            "job_header": job_header_lines,
            "quoted_environment": quoted_environment,
            "quoted_arguments": quoted_arguments,
            "executable": self.executable,
        }

    async def start(self):
        """Start workers and point them to our local scheduler"""
        logger.info("Starting worker: %s", self.name)

        if "initialdir" not in self.job_header_dict:
            raise RuntimeError("Attempting to start a job before files are prepared")

        job = self.job_script()
        logger.debug(job)
        job = htcondor.Submit(job)

        def sub():
            try:
                classads = []
                with SCHEDD.transaction() as txn:
                    cluster_id = job.queue(txn, ad_results=classads)

                logger.debug(f"ClassAds for job {cluster_id}: {classads}")
                SCHEDD.spool(classads)
                return cluster_id
            except htcondor.HTCondorInternalError as ex:
                logger.error(str(ex))
                return None
            except htcondor.HTCondorIOError as ex:
                logger.error(str(ex))
                return None

        self.job_id = await asyncio.get_event_loop().run_in_executor(SCHEDD_POOL, sub)
        if self.job_id:
            self.known_jobs.add(self.job_id)
            weakref.finalize(self, self._close_job, self.job_id)

            logger.info(f"Starting job: {self.job_id} for worker {self.name}")
            # dask_jobqueue Job class does some things we don't want
            # so we do what is done in distributed.ProcessInterface
            self.status = Status.running

    async def close(self):
        if self.status in (Status.closing, Status.closed):
            return await self.finished()
        logger.info(
            f"Closing worker {self.name} job_id {self.job_id} (current status: {self.status})"
        )
        self.status = Status.closing
        if self._cluster:
            # workaround for https://github.com/dask/distributed/issues/4532
            ret = await self._cluster().scheduler_comm.retire_workers(
                names=[self.name], remove=True, close_workers=True
            )
            # adaptive cluster scaling seems to call this properly already, so may be a no-op
            logger.debug(f"Worker {self.name} retirement info: {ret}")

        def check_gone():
            try:
                return len(SCHEDD.query(f"ClusterId == {self.job_id}")) == 0
            except htcondor.HTCondorIOError as ex:
                logger.error(str(ex))
                return False

        for _ in range(30):
            await asyncio.sleep(1)
            try:
                is_gone = await asyncio.get_event_loop().run_in_executor(
                    SCHEDD_POOL, check_gone
                )
            except RuntimeError as ex:
                if str(ex) == "cannot schedule new futures after interpreter shutdown":
                    logger.info(f"Thread pool lost while checking worker {self.name} job {self.job_id}")
                    # We're not going to be able to do anything async now
                    self.status = None
                    self._event_finished.set()
                    return
                raise ex
            if is_gone:
                logger.info(f"Gracefully closed worker {self.name} job {self.job_id}")
                self.known_jobs.discard(self.job_id)
                self.status = Status.closed
                self._event_finished.set()
                return

        logger.info(
            f"Reached timeout, forcefully stopping worker: {self.name} job: {self.job_id}"
        )

        def stop():
            try:
                res = SCHEDD.act(
                    htcondor.JobAction.Remove, f"ClusterId == {self.job_id}"
                )
                if res["TotalSuccess"] == 1 and res["TotalChangedAds"] == 1:
                    return True
            except htcondor.HTCondorIOError as ex:
                logger.error(str(ex))
            return False

        result = await asyncio.get_event_loop().run_in_executor(SCHEDD_POOL, stop)
        if result:
            logger.info(f"Forcefully closed job {self.job_id}")
            self.known_jobs.discard(self.job_id)
            self.status = Status.closed
            self._event_finished.set()
            return
        logger.error(f"Failed to forcefully close job {self.job_id}")
        self.status = None
        self._event_finished.set()

    @classmethod
    def _close_job(cls, job_id):
        if job_id in cls.known_jobs:
            logger.warning(
                f"Last-ditch attempt to close HTCondor job {job_id} in finalizer! You should confirm the job exits!"
            )
            try:
                SCHEDD.act(htcondor.JobAction.Remove, f"ClusterId == {job_id}")
            except htcondor.HTCondorIOError as ex:
                logger.error(str(ex))
            cls.known_jobs.discard(job_id)


class LPCGatewayCluster(LPCCondorCluster):
    __doc__ = (
        HTCondorCluster.__doc__
        + """

    Additional LPC parameters:
    ship_env: bool
        If True (default False), ship the ``/srv/.env`` virtualenv with the job and
        run workers from that environent. This allows user-installed packages
        to be available on the worker
    image: str
        Name of the singularity image to use (default: $COFFEA_IMAGE)
    transfer_input_files: str, List[str]
        Files to be shipped along with the job. They will be placed in the
        working directory of the workers, as usual for HTCondor. Any paths
        not accessible from the LPC schedds (because of restrictions placed
        on remote job submission) will be copied to a temporary directory
        under ``/uscmst1b_scratch/lpc1/3DayLifetime/$USER``.
    """
    )
    job_cls = LPCGatewayCondorJob
    config_name = "lpccondor"
    schedd_safe_paths = [
        os.path.expanduser("~"),
        "/uscmst1b_scratch/lpc1/3DayLifetime",
        "/uscms_data",
    ]

    def __init__(self, **kwargs):
        # These settings are static
        gateway = Gateway(
            address="http://172.30.227.32",
            auth='jupyterhub',
        )
#        kwargs.setdefault(
#            "image", os.environ.get("COFFEA_IMAGE", "coffeateam/coffea-dask:latest")
#        )
        self._transfer_input_files = []

    def _build_scratch(self):
        username = pwd.getpwuid( os.getuid() )[ 0 ]
        security = self.cluster.security
        tmproot = f"/uscmst1b_scratch/lpc1/3DayLifetime/{username}/{cluster_name}"
        condor_logdir = f"{tmproot}/condor"
        credentials_dir = f"{tmproot}/dask-credentials"
        worker_space_dir = f"{tmproot}/dask-worker-space"
        image_name = f"/cvmfs/unpacked.cern.ch/registry.hub.docker.com/coffeateam/coffea-dask-cc7-gateway:0.7.12-fastjet-3.3.4.0rc9-g8a990fa"
        os.makedirs(tmproot, exist_ok=True)
        os.makedirs(condor_logdir, exist_ok=True)
        os.makedirs(credentials_dir, exist_ok=True)
        os.makedirs(worker_space_dir, exist_ok=True)
    
        with open(f"{credentials_dir}/dask.crt", 'w') as f:
            f.write(security.tls_cert)
        with open(f"{credentials_dir}/dask.pem", 'w') as f:
            f.write(security.tls_key)
        with open(f"{credentials_dir}/api-token", 'w') as f:
            f.write(os.environ['JUPYTERHUB_API_TOKEN'])

        self.tmproot = tmproot

    def _clean_scratch(self):
        if self.tmproot is not None:
            self.tmproot.cleanup()

    async def _start(self):
        try:
            cluster = await gateway.new_cluster(**kwargs)
        except Exception:
            print(traceback.format_exc())

        workers_homedir = await self.loop.run_in_executor(
            None, self._build_scratch
        )
        print(workers_homedir)
        self._job_kwargs.setdefault("job_extra", {})
        self._job_kwargs["job_extra"]["initialdir"] = self.scratch_area.name
        self._job_kwargs["job_extra"]["transfer_input_files"] = ",".join(
            prepared_input_files
        )

    async def _close(self):
        await super()._close()
        await self.loop.run_in_executor(None, self._clean_scratch)