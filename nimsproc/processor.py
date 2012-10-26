#!/usr/bin/env python
#
# @author:  Reno Bowen
#           Gunnar Schaefer

import os
import abc
import glob
import time
import shutil
import signal
import argparse
import threading

import sqlalchemy
import transaction

import nimsutil
from nimsgears.model import *


class Processor(object):

    def __init__(self, db_uri, nims_path, physio_path, task, filters, log, max_jobs, reset, sleeptime):
        super(Processor, self).__init__()
        self.nims_path = nims_path
        self.physio_path = physio_path
        self.task = unicode(task) if task else None
        self.filters = filters
        self.log = log
        self.max_jobs = max_jobs
        self.sleeptime = sleeptime

        self.alive = True
        init_model(sqlalchemy.create_engine(db_uri))
        if reset: self.reset_all()

    def halt(self):
        self.alive = False

    def run(self):
        while self.alive:
            if threading.active_count()-1 < self.max_jobs:
                query = Job.query.join(DataContainer).join(Epoch)
                if self.task:
                    query = query.filter(Job.task==self.task)
                for f in self.filters:
                    query = query.filter(eval(f))
                job = query.filter(Job.status==u'pending').order_by(Job.id).with_lockmode('update').first()

                if job:
                    if isinstance(job.data_container, Epoch):
                        ds = job.data_container.primary_dataset
                        if ds.filetype == nimsutil.dicomutil.DicomFile.filetype:
                            pipeline_class = DicomPipeline
                        elif ds.filetype == nimsutil.pfile.PFile.filetype:
                            pipeline_class = PFilePipeline

                    pipeline = pipeline_class(job, self.nims_path, self.physio_path, self.log)
                    job.status = u'running'
                    transaction.commit()
                    pipeline.start()
                else:
                    self.log.debug('Waiting for work...')
                    time.sleep(self.sleeptime)
            else:
                self.log.debug('Waiting for jobs to finish...')
                time.sleep(self.sleeptime)

    def reset_all(self):
        """Reset all active jobs to new."""
        job_query = Job.query.filter((Job.status != u'pending') & (Job.status != u'done') & (Job.status != u'abandoned'))
        if self.task:
            job_query = job_query.filter(Job.task==self.task)
        for job in job_query.all():
            job.status = u'pending'
            job.activity = u'reset to pending'
            self.log.info(u'%d %s %s' % (job.id, job, job.activity))
        transaction.commit()


class Pipeline(threading.Thread):

    __metaclass__ = abc.ABCMeta

    def __init__(self, job, nims_path, physio_path, log):
        super(Pipeline, self).__init__()
        self.job = job
        self.nims_path = nims_path
        self.physio_path = physio_path
        self.log = log

    def run(self):
        DBSession.add(self.job)
        self.job.activity = u'started'
        self.log.info(u'%d %s %s' % (self.job.id, self.job, self.job.activity))
        transaction.commit()
        DBSession.add(self.job)
        try:
            restart_job = False
            if self.job.task == u'find&proc':
                self.find()
                self.process()
            elif self.job.task == u'find':
                self.find()
            elif self.job.task == u'proc':
                self.process()
        except Exception as ex:
            if self.job.status == u'restarting':
                restart_job = True
            self.job.status = u'failed'
            self.job.activity = u'failed: %s' % ex
            self.log.warning(u'%d %s %s' % (self.job.id, self.job, self.job.activity))
        else:
            if self.job.status == u'restarting':
                restart_job = True
            self.job.status = u'done'
            self.job.activity = u'done'
            self.log.info(u'%d %s %s' % (self.job.id, self.job, self.job.activity))
        finally:
            if restart_job:
                self.job.status = u'pending'
                self.job.activity = u'restarted'
                self.log.info(u'%d %s %s' % (self.job.id, self.job, self.job.activity))
        transaction.commit()

    def clean(self, data_container, kind):
        for ds in Dataset.query.filter_by(container=data_container).filter_by(kind=kind).all():
            shutil.rmtree(os.path.join(self.nims_path, ds.relpath))
            ds.delete()

    @abc.abstractmethod
    def find(self):
        self.clean(self.job.data_container, u'peripheral')
        transaction.commit()
        DBSession.add(self.job)

        dc = self.job.data_container
        ds = self.job.data_container.primary_dataset
        if dc.physio_flag:
            physio_files = nimsutil.find_ge_physio(self.physio_path, dc.timestamp+dc.duration, dc.psd.encode('utf-8'))
            if physio_files:
                self.job.activity = u'physio found %s' % (', '.join([os.path.basename(pf) for pf in physio_files]))
                self.log.info(u'%d %s %s' % (self.job.id, self.job, self.job.activity))
                dataset = Dataset.at_path(self.nims_path, u'physio')
                DBSession.add(self.job)
                DBSession.add(self.job.data_container)
                dataset.file_cnt_act = 0
                dataset.file_cnt_tgt = len(physio_files)
                dataset.kind = u'peripheral'
                dataset.container = self.job.data_container
                for f in physio_files:
                    shutil.copy2(f, os.path.join(self.nims_path, dataset.relpath))
                    dataset.file_cnt_act += 1
            else:
                self.job.activity = u'no physio files found'
                self.log.info(u'%d %s %s' % (self.job.id, self.job, self.job.activity))
        else:
            self.job.activity = u'physio not recorded'
            self.log.info(u'%d %s %s' % (self.job.id, self.job, self.job.activity))
        transaction.commit()
        DBSession.add(self.job)

    @abc.abstractmethod
    def process(self):
        self.clean(self.job.data_container, u'derived')
        self.job.activity = u'generating NIfTI / running recon'
        self.log.info(u'%d %s %s' % (self.job.id, self.job, self.job.activity))
        transaction.commit()
        DBSession.add(self.job)


class DicomPipeline(Pipeline):

    def find(self):
        return super(DicomPipeline, self).find()

    def process(self):
        super(DicomPipeline, self).process()

        ds = self.job.data_container.primary_dataset
        with nimsutil.TempDirectory() as outputdir:
            outbase = os.path.join(outputdir, ds.container.name)
            dcm_tgz = os.path.join(self.nims_path, ds.relpath, os.listdir(os.path.join(self.nims_path, ds.relpath))[0])
            dcm_acq = nimsutil.dicomutil.DicomAcquisition(dcm_tgz, self.log)
            conv_res = dcm_acq.convert(outbase)

            if conv_res:
                outputdir_list = os.listdir(outputdir)
                self.job.activity = u'generated %s' % (', '.join([f for f in outputdir_list]))
                self.log.info(u'%d %s %s' % (self.job.id, self.job, self.job.activity))
                conv_ds = Dataset.at_path(self.nims_path, u'bitmap' if conv_res == 'bitmap' else u'nifti')
                DBSession.add(self.job)
                DBSession.add(self.job.data_container)

                conv_ds.file_cnt_act = 0
                conv_ds.file_cnt_tgt = len(outputdir_list)
                conv_ds.kind = u'derived'
                conv_ds.container = self.job.data_container
                for f in outputdir_list:
                    shutil.copy2(os.path.join(outputdir, f), os.path.join(self.nims_path, conv_ds.relpath))
                    conv_ds.file_cnt_act += 1
                transaction.commit()

            if conv_res != 'bitmap':
                pyramid_ds = Dataset.at_path(self.nims_path, u'img_pyr')
                DBSession.add(self.job)
                DBSession.add(self.job.data_container)
                nimsutil.pyramid.ImagePyramid(conv_res, log=self.log).generate(os.path.join(self.nims_path, pyramid_ds.relpath))
                self.job.activity = u'image pyramid generated'
                self.log.info(u'%d %s %s' % (self.job.id, self.job, self.job.activity))
                pyramid_ds.kind = u'derived'
                pyramid_ds.container = self.job.data_container
                transaction.commit()

        DBSession.add(self.job)


class PFilePipeline(Pipeline):

    def find(self):
        return super(PFilePipeline, self).find()

    def process(self):
        super(PFilePipeline, self).process()

        ds = self.job.data_container.primary_dataset
        with nimsutil.TempDirectory() as outputdir:
            for pfile in os.listdir(os.path.join(self.nims_path, ds.relpath)):
                try:
                    pf = nimsutil.pfile.PFile(os.path.join(self.nims_path, ds.relpath, pfile), self.log)
                except nimsutil.pfile.PFileError:
                    pf = None
                else:
                    break
            conv_res = pf.to_nii(os.path.join(outputdir, ds.container.name))

            if conv_res:
                outputdir_list = os.listdir(outputdir)
                self.job.activity = u'generated %s' % (', '.join([f for f in outputdir_list]))
                self.log.info(u'%d %s %s' % (self.job.id, self.job, self.job.activity))
                dataset = Dataset.at_path(self.nims_path, u'nifti')
                DBSession.add(self.job)
                DBSession.add(self.job.data_container)
                dataset.file_cnt_act = 0
                dataset.file_cnt_tgt = len(outputdir_list)
                dataset.kind = u'derived'
                dataset.container = self.job.data_container
                for f in outputdir_list:
                    shutil.copy2(os.path.join(outputdir, f), os.path.join(self.nims_path, dataset.relpath))
                    dataset.file_cnt_act += 1
                transaction.commit()

                pyramid_ds = Dataset.at_path(self.nims_path, u'img_pyr')
                DBSession.add(self.job)
                DBSession.add(self.job.data_container)
                nimsutil.pyramid.ImagePyramid(conv_res, log=self.log).generate(os.path.join(self.nims_path, pyramid_ds.relpath))
                self.job.activity = u'image pyramid generated'
                self.log.info(u'%d %s %s' % (self.job.id, self.job, self.job.activity))
                pyramid_ds.kind = u'derived'
                pyramid_ds.container = self.job.data_container
                transaction.commit()

        DBSession.add(self.job)


class ArgumentParser(argparse.ArgumentParser):

    def __init__(self):
        super(ArgumentParser, self).__init__()
        self.add_argument('db_uri', metavar='URI', help='database URI')
        self.add_argument('nims_path', metavar='DATA_PATH', help='data location')
        self.add_argument('physio_path', metavar='PHYSIO_PATH', help='path to physio data')
        self.add_argument('-t', '--task', help='find|proc  (default is all)')
        self.add_argument('-e', '--filter', default=[], action='append', help='sqlalchemy filter expression')
        self.add_argument('-j', '--jobs', type=int, default=1, help='maximum number of concurrent threads')
        self.add_argument('-r', '--reset', action='store_true', help='reset currently active (crashed) jobs')
        self.add_argument('-s', '--sleeptime', type=int, default=10, help='time to sleep between db queries')
        self.add_argument('-n', '--logname', default=os.path.splitext(os.path.basename(__file__))[0], help='process name for log')
        self.add_argument('-f', '--logfile', help='path to log file')
        self.add_argument('-l', '--loglevel', default='info', help='path to log file')


if __name__ == '__main__':
    # workaround for http://bugs.python.org/issue7980
    import datetime # used in nimsutil
    datetime.datetime.strptime('0', '%S')

    args = ArgumentParser().parse_args()
    log = nimsutil.get_logger(args.logname, args.logfile, args.loglevel)
    processor = Processor(args.db_uri, args.nims_path, args.physio_path, args.task, args.filter, log, args.jobs, args.reset, args.sleeptime)

    def term_handler(signum, stack):
        processor.halt()
        log.info('Receieved SIGTERM - shutting down...')
    signal.signal(signal.SIGTERM, term_handler)

    processor.run()
    log.warning('Process halted')
