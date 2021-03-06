"""
Module to handle jenkins server.
"""
import os
import datetime
import time
import logging
import threading
from collections import defaultdict
import jenkins
import requests


handler = logging.StreamHandler()
formatter = logging.Formatter("[%(asctime)s] %(levelname)s:%(name)s:%(message)s")
handler.setFormatter(formatter)
logger = logging.getLogger()
logger.setLevel("INFO")
logger.addHandler(handler)


def retry_on_errors(exceptions_to_catch, count=5, timeout=5):
    """
    Decorator to catch errors against Jenkins server and perform retries.

    :param exceptions_to_catch:
    :param count:
    :param timeout:
    :return:
    """
    def func_wrapper(base_func):
        def func_base(*args, **kwargs):
            for i in range(count):
                try:
                    return base_func(*args, **kwargs)
                except exceptions_to_catch:
                    logger.info(f"{base_func.__name__} failed, retry {i} after {timeout} seconds")
                time.sleep(timeout)
            raise TimeoutError(f"count: {count} timeout: {timeout}")
        return func_base
    return func_wrapper


class JenkinsManager:
    """
    Perform several tasks against Jenkins server.
    Main task- trigger multiple jobs and follow their execution.
    """
    JOBS_START_TIMEOUT = 20 * 60
    JOBS_FINISH_TIMEOUT = 20 * 60
    SLEEP_TIME = 5
    BUILDS_PER_JOB = defaultdict(lambda: defaultdict(lambda: None))

    def __init__(self, jenkins_address, username, password, protocol="https", port="443", timeout=10):
        self.server = jenkins.Jenkins(f"{protocol}://{jenkins_address}:{port}", username=username, password=password, timeout=timeout)
        self.hostname = jenkins_address

    def execute_jobs(self, jobs):
        """
        Function to trigger a flow of multiple jobs' execution. At the end prints the failed jobs.
        :param jobs:
        :return:
        """
        self.set_jobs_current_builds(jobs)
        self.trigger_jobs(jobs)
        self.wait_for_builds_to_start_execution(jobs)
        self.wait_for_builds_to_finish_execution(jobs)
        return self.report_results(jobs)

    def set_jobs_current_builds(self, jobs):
        """
        Set current build_ids for all jobs.
        They will be used as a baseline for searching the build_its-
        since the build_id is incrementing no way it will be less than this number.
        :param jobs:
        :return:
        """
        for job in jobs:
            if job.name not in self.BUILDS_PER_JOB:
                self.BUILDS_PER_JOB[job.name][self.get_next_build_number(job.name)] = None

    def trigger_jobs(self, jobs, multithreaded=True):
        """
        Trigger all jobs.
        :param jobs:
        :param multithreaded: If True- triggers each job in a separate thread.
        :return:
        """
        start_time = datetime.datetime.now()
        logger.info("Begin triggering jobs")
        for job in jobs:
            if multithreaded:
                single_job_thread = threading.Thread(target=self.thread_trigger_job, args=(job,))
                single_job_thread.start()
            else:
                self.thread_trigger_job(job)

        logger.info(f"Finished triggering jobs took {datetime.datetime.now() - start_time}")

    @retry_on_errors((requests.exceptions.ConnectionError,), count=5, timeout=5)
    def thread_trigger_job(self, job):
        """
        Function to trigger single job.
        :param job:
        :return:
        """
        parameters = job.get_request_parameters()
        logger.info(f"Triggering job {job.name} with parameters {parameters}")
        queue_item_id = self.server.build_job(job.name, parameters=parameters)
        job.queue_item_id = queue_item_id
        logger.info(f"Triggered job with queue_id: {queue_item_id}")

    def wait_for_builds_to_start_execution(self, jobs):
        """
        Wait for triggered jobs to exit the waiting queue and start execution.
        :param jobs:
        :return:
        """
        start_time = datetime.datetime.now()

        while any([job.build_id is None for job in jobs]) and (
                datetime.datetime.now() - start_time).seconds < self.JOBS_START_TIMEOUT:

            for job in jobs:
                if job.queue_item_id is None:
                    continue

                if job.build_id is not None:
                    continue

                try:
                    self.update_job_build_id_by_queue_id(job)
                except jenkins.JenkinsException as exception_received:
                    if f"queue number[{job.queue_item_id}] does not exist" not in repr(exception_received):
                        raise
                    logger.info(f"Queue item '{job.queue_item_id}' does not exist anymore updating with parameter")
                    self.update_job_build_id_by_parameter_uid(job)

            if all([job.build_id is not None for job in jobs]):
                break

            logger.info(f"wait_for_builds_to_start_execution going to sleep for: {self.SLEEP_TIME}")
            time.sleep(self.SLEEP_TIME)

    @retry_on_errors((requests.exceptions.ConnectionError,), count=5, timeout=5)
    def update_job_build_id_by_queue_id(self, job):
        """
        Find the build Id based on queue item Id.
        :param job:
        :return:
        """
        dict_item = self.server.get_queue_item(job.queue_item_id)
        if "executable" in dict_item:
            job.build_id = dict_item["executable"].get("number")

    @retry_on_errors((requests.exceptions.ConnectionError,), count=5, timeout=5)
    def update_job_build_id_by_parameter_uid(self, job):
        """
        If the queue item was already deleted use the parameter uid to find the build in the history.
        :param job:
        :return:
        """
        if job.uid_parameter_name is None:
            return

        start_build_id = min(list(self.BUILDS_PER_JOB[job.name].keys()))
        end_build_id = self.get_next_build_number(job.name) - 1

        for build_id in range(end_build_id, start_build_id, -1):
            if self.BUILDS_PER_JOB[job.name][build_id] is None:
                self.update_build_status(job.name, build_id)

            uid_parameter_value = self.get_uid_parameter_value_from_build_info(self.BUILDS_PER_JOB[job.name][build_id], job.uid_parameter_name)
            if job.uid == uid_parameter_value:
                job.build_id = build_id
                logger.info(f"Found build uid {build_id}")
                break

    @staticmethod
    def get_uid_parameter_value_from_build_info(build_info, uid_parameter_name):
        """
        Extract the uid_parameter value from build info response.
        :param build_info:
        :param uid_parameter_name:
        :return:
        """
        for action in build_info["actions"]:
            if action["_class"] == "hudson.model.ParametersAction":
                for parameter in action["parameters"]:
                    if parameter["name"] == uid_parameter_name:
                        return parameter["value"]
        return None

    def wait_for_builds_to_finish_execution(self, jobs):
        """
        Wait for all jobs to reach any final status - success, unstable, aborted, failed
        :param jobs:
        :return:
        """
        start_time = datetime.datetime.now()
        while any([job.build_status is None for job in jobs]) and (datetime.datetime.now() - start_time).seconds < self.JOBS_FINISH_TIMEOUT:
            try:
                self.update_builds_statuses(jobs)
            except jenkins.JenkinsException as jenkins_error:
                repr_jenkins_error = repr(jenkins_error)
                if "number[" not in repr_jenkins_error or "does not exist" not in repr_jenkins_error:
                    logger.info(f"wait_for_builds_to_finish_execution waits for jobs to start execution {repr_jenkins_error}")
                    raise

            logger.info(f"wait_for_builds_to_finish_execution going to sleep for: {self.SLEEP_TIME}")

            time.sleep(self.SLEEP_TIME)

    def update_builds_statuses(self, jobs):
        """
        Fetch new builds' statuses for running builds.
        :param jobs:
        :return:
        """
        for job in jobs:
            if job.build_status is not None:
                continue
            if job.build_id is None:
                continue

            job.build_status = self.update_build_status(job.name, job.build_id)
            if job.build_status is not None:
                logger.info(f"Finished execution build_id: {job.build_id} with result: {job.build_status}")

    @retry_on_errors((requests.exceptions.ConnectionError,), count=5, timeout=5)
    def update_build_status(self, job_name, build_id):
        """
        Fetch single build status from Jenkins.
        :param job_name:
        :param build_id:
        :return:
        """
        logger.info(f"Updating build status for job: {job_name} build_id: {build_id}")
        build_info = self.server.get_build_info(job_name, build_id)
        self.BUILDS_PER_JOB[job_name][build_id] = build_info

        build_status = build_info.get("result")

        return build_status

    @retry_on_errors((requests.exceptions.ConnectionError,), count=5, timeout=5)
    def get_job_info(self, job_name):
        """
        Fetch single job's information from Jenkins.

        :param job_name:
        :return:
        """
        job_info = self.server.get_job_info(job_name)

        return job_info

    def get_next_build_number(self, job_name):
        """
        Get the next build number for a specific job.
        :param job_name:
        :return:
        """
        job_info = self.get_job_info(job_name)
        return job_info["nextBuildNumber"]

    def report_results(self, jobs):
        """
        Generate report for finished jobs.
        :param jobs:
        :return:
        """
        report_lines = []
        for job in jobs:
            if job.build_status is None:
                if job.build_id is None:
                    line = f"Error: Job failed to start job: '{job.name}': {job.get_request_parameters()}"
                    report_lines.append(line)
                    continue
                line = f"Error: Waiting timeout reached job: '{job.name}': {self.BUILDS_PER_JOB[job.name][job.build_id]['url']}"
                report_lines.append(line)
            elif job.build_status not in ["SUCCESS"]:
                line = f"Error: Job: '{job.name}' failed to finish [{job.build_status}]: {self.BUILDS_PER_JOB[job.name][job.build_id]['url']}"
                report_lines.append(line)
                continue
        return "\n".join(report_lines)

    def get_job_config(self, job_name):
        """
        Fetch job's configuration. XML string returned
        :param job_name:
        :return:
        """
        str_job_xml = self.server.get_job_config(job_name)
        return str_job_xml

    def save_job_config(self, job_name, file_output):
        """
        Fetch job's configuration and save it to file.
        :param job_name:
        :param file_output:
        :return:
        """
        try:
            str_job_xml = self.get_job_config(job_name)
        except jenkins.NotFoundException as exception_received:
            logger.error(repr(exception_received))
            return

        with open(file_output, "w+") as file_handler:
            file_handler.write(str_job_xml)

    @retry_on_errors((requests.exceptions.HTTPError,), count=5, timeout=5)
    def create_job(self, job, file_input):
        """
        Create a job with specific name and xml configs file.
        :param job:
        :param file_input:
        :return:
        """
        with open(file_input) as file_handler:
            str_job_xml = file_handler.read()

        logger.info(f"Creating job {job.name} from {file_input}")
        try:
            self.server.create_job(job.name, str_job_xml)
        except jenkins.JenkinsException as exception_received:
            if f"job[{job.name}] already exists" not in repr(exception_received):
                raise
            logger.info(repr(exception_received))

    @retry_on_errors((requests.exceptions.HTTPError,), count=5, timeout=5)
    def delete_jobs(self, jobs):
        """
        Delete jobs - ignore not existing jobs.
        :param jobs:
        :return:
        """
        for job in jobs:
            logger.info(f"Deleting job {job.name}")
            try:
                self.server.delete_job(job.name)
            except jenkins.NotFoundException as exception_received:
                logger.info(repr(exception_received))

    def cleanup(self):
        """
        Bonus function - if you get many dirty jobs (qa/stg) this function can help you.
        1) Jobs not being executed for 30 days or more.
        2) Jobs with 100 last failed executions.

        :return:
        """
        jobs = self.get_all_jobs()
        now = datetime.datetime.now()
        time_limit = datetime.timedelta(days=30)
        lst_ret = []
        lst_ret_exceeded_time = []
        for job in jobs:
            try:
                job_info = self.get_job_info(job["name"])
            except jenkins.JenkinsException as exception_received:
                if "does not exist" in repr(exception_received):
                    lst_ret.append(repr(exception_received))
                    continue
                raise

            last_build = job_info.get("lastBuild")
            if last_build is None:
                lst_ret.append(f"{job['name']}: Never run")
                continue

            build_info = self.server.get_build_info(job["name"], last_build["number"])
            last_build_date = datetime.datetime.fromtimestamp(build_info["timestamp"]/1000)
            if now - last_build_date > time_limit:
                report_line = f"{job['name']}: last_build_date {(now - last_build_date).days} days ago"
                lst_ret_exceeded_time.append((report_line, (now - last_build_date).days))
            else:
                for build in job_info["builds"]:
                    self.update_build_status(job["name"], build["number"])
                    if self.BUILDS_PER_JOB[job["name"]][build["number"]]["result"] == "SUCCESS":
                        break
                else:
                    lst_ret.append(f"{job['name']}: last {len(job_info['builds'])} build were not SUCCESS")

        lst_ret = [x[0] for x in sorted(lst_ret_exceeded_time, key=lambda x: x[1], reverse=True)] + lst_ret
        return "\n".join(lst_ret)

    @retry_on_errors((requests.exceptions.ConnectionError,), count=5, timeout=5)
    def get_all_jobs(self):
        """
        Get all jobs' overview information.
        :return:
        """
        job_dicts = self.server.get_all_jobs()
        return job_dicts

    def backup_jobs(self, backups_dir):
        """
        Save all jobs' cnfigs in separate files.

        :param backups_dir:
        :return:
        """
        os.makedirs(backups_dir, exist_ok=True)

        backup_dir_name = self.hostname.replace(".", "_")
        backup_dir_path = os.path.join(backups_dir, backup_dir_name)
        os.makedirs(backup_dir_path, exist_ok=True)
        for job in self.get_all_jobs():
            logger.info(f"Start backing up job {job['name']}")
            self.save_job_config(job["name"], os.path.join(backup_dir_path, f"{job['name']}.xml"))
            logger.info(f"End backing up job '{job['name']}'")
