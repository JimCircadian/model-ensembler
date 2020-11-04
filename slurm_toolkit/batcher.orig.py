import logging
import os
import threading
import time

import slurm_toolkit

from .tasks import submit as slurm_submit
from .tasks.utils import execute_command
from .utils import Arguments

import jinja2

from pyslurm import config, job


# TODO: Re-architect to ensure this is thread-safe
class Run(object):
    run_sem = None

    def __init__(self, batch, runid, rundir, runargs=None):
        self.runid = runid
        self.dir = rundir

        self.slurm_id = None
        self.slurm_running = False
        self.slurm_state = None
        self.slurm_ready = False

        # TODO: Not really the best way of doing this, use some appropriate typing for all the data used
        batch_dict = batch._asdict()
        for k, v in batch_dict.items():
            if not k.endswith("flight") and k != "runs" and k != "name":
                setattr(self, "{}".format(k), v)

        if runargs:
            self.__dict__.update(runargs)

        self.running = False
        self.finished = False
        self.batch = batch

        if not Run.run_sem:
            Run.run_sem = threading.Semaphore(value=batch.maxruns)

        self._thread = threading.Thread(name=runid, target=self.run)

    def run(self):
        logging.info("Starting ")

        while not self.slurm_ready:
            logging.info("HPC not ready for {}".format(self.runid))
            time.sleep(60.)

        # TODO: We're running through a sequence of checks & tasks, interleaved now.
        ####### START
        with Run.run_sem:
            self.run_flight_items(self.batch.preflight)
        ####### END

        if Arguments().nosubmission:
            logging.info("Skipping actual slurm submission based on arguments")
            self.slurm_id = self.name
        else:
            self.slurm_id = slurm_submit(self, script=self.batch.job_file)

            while not self.slurm_running:
                try:
                    self.slurm_state = job().find_id(int(self.slurm_id))[0]['job_state']
                except ValueError:
                    logging.warning("Job {} not registered yet".format(self.slurm_id))

                if self.slurm_state and (self.slurm_state in (
                        "COMPLETING", "PENDING", "RESV_DEL_HOLD", "RUNNING", "SUSPENDED"
                        "RUNNING", "COMPLETED", "FAILED", "CANCELLED")):
                    self.slurm_running = True
                else:
                    # TODO: Configurable sleeps please!
                    time.sleep(2.)

            while self.running:
                self.slurm_state = job().find_id(int(self.slurm_id))[0]['job_state']
                logging.info("{} monitor got state {} for job {}".format(
                    self.runid, self.slurm_state, self.slurm_id))

                if self.slurm_state in ("COMPLETED", "FAILED", "CANCELLED"):
                    break
                else:
                    time.sleep(10.)

        self.running = False

        ####### START
        with Run.run_sem:
            self.run_flight_items(self.batch.postflight)
        ####### END

        self.finished = True

    def submit(self):
        self.running = True

        self._thread.start()




# TODO: Work on multi-batches
class Executor(object):
    def __init__(self, configuration):
        self._cfg = configuration
        self._args = Arguments()
        self.__active = None
        # TODO: Needs to be thread safe for multiple batches
        self.runs = dict()

    def run(self):
        logging.info("Running batcher")

        for batch in self._cfg.batches:
            self.__active = batch
            self.runs[batch.name] = list()

            for run in batch.runs:
                runid = "{}-{}".format(self.active.name, batch.runs.index(run))
                self.runs[batch.name].append(Run(batch=batch,
                                                 runid=runid,
                                                 rundir=os.path.join(self.active.basedir, runid),
                                                 runargs=run))

            for run in self.runs[batch.name]:
                self.prep_hpc_job(run)

                run.submit()

                active = len([r for r in self.runs[batch.name] if r.running and not r.finished])
                while active >= batch.maxruns:
                    logging.info("Waiting for number of running threads to diminish {} ({})".format(
                        active, batch.maxruns))
                    time.sleep(10.)
                    active = len([r for r in self.runs[batch.name] if r.running and not r.finished])

                run.slurm_ready = True

            self.__active = None

    def prep_hpc_job(self, run):
        if not os.path.exists(self.active.basedir):
            raise ActiveBatchException("No basedir to process batch in!")
        os.chdir(self.active.basedir)

        # Template out the slurm runner

        if os.path.exists(run.dir):
            raise ActiveBatchException("Run directory {} already exists".format(run.dir))

        os.mkdir(run.dir, mode=0o775)

        sync = execute_command("rsync -aXE {}/ {}/".format(self.active.template_dir, run.dir))
        if sync.returncode != 0:
            raise ActiveBatchException("Could not grab template directory {} to {}".format(
                self.active.template_dir, run.dir
            ))

        for tmpl_file in self.active.template:
            if tmpl_file[-3:] != ".j2":
                raise ActiveBatchException("{} doe not appear to be a Jinja2 template (.j2)".format(tmpl_file))

            tmpl_path = os.path.join(run.dir, tmpl_file)
            with open(tmpl_path , "r") as fh:
                tmpl_data = fh.read()

            dst_file = tmpl_path[:-3]
            logging.info("Templating {} to {}".format(tmpl_path, dst_file))
            tmpl = jinja2.Template(tmpl_data)
            dst_data = tmpl.render(run=vars(run))
            with open(dst_file, "w+") as fh:
                fh.write(dst_data)
            os.chmod(dst_file, os.stat(tmpl_path).st_mode)

            os.unlink(tmpl_path)

    @property
    def active(self):
        if not self.__active:
            raise NoBatchException
        return self.__active

    @active.setter
    def active(self, b):
        if self.__active:
            raise ActiveBatchException
        self.__active = b


class CheckException(Exception):
    pass


class TaskException(Exception):
    pass


class ProcessingException(Exception):
    pass


class NoBatchException(Exception):
    pass


class ActiveBatchException(Exception):
    pass
