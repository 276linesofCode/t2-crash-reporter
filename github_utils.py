import json
import logging

from google.appengine.api import memcache
from google.appengine.ext import deferred

from github import Github
from model import CrashReport, GlobalPreferences
from util import is_appengine_local, crash_uri, CrashReports

# constants
TOKEN_KEY = 'github_token'
WEBHOOK_SECRET = 'webhook_secret'

DEBUG_OWNER = 'tikurahul'
DEBUG_REPO = 'sandbox'
DEBUG_CRASH_REPORTER_HOST = 'http://localhost:8080'

OWNER = 'tessel'
REPO = 't2-cli'
CRASH_REPORTER_HOST = 'http://crash-reporter.tessel.io'

DEBUG_CLIENT_SECRETS = 'debug_client_secrets.json'
CLIENT_SECRETS = 'client_secrets.json'


def issue_url(issue_number):
    """
    Returns the GitHub issue URL.
    """
    if is_appengine_local():
        repo_name = '{0}/{1}'.format(DEBUG_OWNER, DEBUG_REPO)
    else:
        repo_name = '{0}/{1}'.format(OWNER, REPO)

    return 'https://github.com/{0}/issues/{1}'.format(repo_name, issue_number)


class GithubOrchestrator(object):
    """
    Orchestrates all communication with GitHub via a task queue.
    """
    __QUEUE__ = 'github-queue'
    # Every x times that we need to update the task with a comment
    __NOTIFY_FREQUENCY__ = 10
    # seconds for recursive enqueue
    __SCHEDULE_DELAY__ = 10

    @classmethod
    def backoff_crash_key_new_crash(cls, crash_report):
        return 'github_task_new_crash_{0}'.format(crash_report.fingerprint)

    @classmethod
    def backoff_crash_key_new_comment(cls, crash_report):
        return 'github_task_new_comment_{0}'.format(crash_report.fingerprint)

    @classmethod
    def manage_github_issue(cls, crash_report):
        """
        Manages the GitHub issue.
        """
        # check global github preference
        preference_value = GlobalPreferences.get_property(GlobalPreferences.__INTEGRATE_WITH_GITHUB__, 'true')
        if preference_value != 'true':
            logging.info('GitHub integration is turned off. Ignoring request.')
            return

        if crash_report is not None:
            issue = crash_report.issue
            count = CrashReport.get_count(crash_report.name)
            if issue is None:
                # new crash
                cls.new_crash_with_backoff(crash_report)
            elif count > 0 and count % GithubOrchestrator.__NOTIFY_FREQUENCY__ == 0:
                # add comments for an existing crash
                cls.new_comment_with_backoff(crash_report)
            else:
                logging.debug('No pending tasks for fingerprint {0}.'.format(crash_report.fingerprint))

    @classmethod
    def new_crash_with_backoff(cls, crash_report):
        """
        there is a chance that we get a new crash before an issue was submitted before.
        """
        backoff_cache_key = cls.backoff_crash_key_new_crash(crash_report)
        backoff_value = memcache.get(backoff_cache_key)
        if not backoff_value:
            # A task does not exist. Queue a job.
            memcache.set(backoff_cache_key, "in_progress")
            deferred.defer(
                GithubOrchestrator.create_issue_job,
                crash_report.fingerprint, _queue=GithubOrchestrator.__QUEUE__)
            logging.info(
                'Enqueued job for new issue on GitHub for fingerprint {0}'.format(crash_report.fingerprint))
        else:
            # task already in progress, backoff
            logging.info(
                'A GitHub task is already in progress. Waiting to the dust to settle for fingerprint {0}'
                    .format(crash_report.fingerprint)
            )

    @classmethod
    def new_comment_with_backoff(cls, crash_report):
        """
        there is a chance that this is a hot issue, and that there are too many crashes coming in.
        try and use backoff, when you are posting a new comment.
        """
        backoff_cache_key = cls.backoff_crash_key_new_comment(crash_report)
        backoff_value = memcache.get(backoff_cache_key)
        if not backoff_value:
            # A task does not exist. Queue a job.
            memcache.set(backoff_cache_key, "in_progress")
            deferred.defer(
                GithubOrchestrator.add_comment_job,
                crash_report.fingerprint, _queue=GithubOrchestrator.__QUEUE__)
            logging.info(
                'Enqueued job for new comment on GitHub for fingerprint {0}'.format(crash_report.fingerprint))
        else:
            # task already in progress, backoff
            logging.info(
                'A GitHub task is already in progress. Waiting to the dust to settle for fingerprint {0}'
                    .format(crash_report.fingerprint)
            )

    @classmethod
    def manage_github_issue_as_task(cls, fingerprint):
        """
        Github Management API as a task.
        """
        crash_report = CrashReport.get_crash(fingerprint)
        GithubOrchestrator.manage_github_issue(crash_report)

    @classmethod
    def create_issue_job(cls, fingerprint):
        """
        Handles the create issue job.
        """
        crash_report = None
        try:
            github_client = GithubClient()
            crash_report = CrashReport.get_crash(fingerprint)
            if crash_report is not None:
                # create the github issue
                issue = github_client.create_issue(crash_report)
                logging.info(
                    'Created GitHub Issue No({0}) for crash ({1})'.format(issue.number, crash_report.fingerprint))
                # update the crash report with the issue id
                updated_report = CrashReports.update_crash_report(crash_report.fingerprint, {
                    # convert to unicode string
                    'issue': str(issue.number)
                })
                logging.info(
                    'Updating crash report with fingerprint ({0}) complete.'.format(updated_report.fingerprint))
        except Exception, e:
            logging.error('Error creating issue for fingerprint ({0}) [{1}]'.format(fingerprint, str(e)))
        finally:
            # remove the backoff cache key, so future jobs may be enqueued
            backoff_cache_key = cls.backoff_crash_key_new_crash(crash_report)
            memcache.delete(backoff_cache_key)

    @classmethod
    def add_comment_job(cls, fingerprint):
        """
        Handles the create comment job
        """
        crash_report = None
        try:
            github_client = GithubClient()
            crash_report = CrashReport.get_crash(fingerprint)
            if crash_report is not None:
                github_client.create_comment(crash_report)
        except Exception, e:
            logging.error('Error creating comment for fingerprint ({0}) [{1}]'.format(fingerprint, str(e)))
        finally:
            # remove the backoff cache key, so future jobs may be enqueued
            backoff_cache_key = cls.backoff_crash_key_new_comment(crash_report)
            memcache.delete(backoff_cache_key)


class GithubClient(object):
    """
    A set of github utilities.
    """

    @classmethod
    def issue_title(cls, crash_report=None):
        crash = crash_report.crash
        lines = [line for line in crash.splitlines(True) if len(line) > 0]
        return 'Crash report: {0}'.format(lines[0])

    def issue_body(self, crash_report):
        crash = crash_report.crash.encode('ascii', 'ignore')
        fingerprint = crash_report.fingerprint
        crash_report_uri = '{0}{1}'.format(self.reporter_host, crash_uri(fingerprint))
        body = '```\n{0}\n```\n\nFull report is at [{1}]({2})'.format(crash, fingerprint, crash_report_uri)
        return body

    @classmethod
    def issue_comment(cls, count):
        new_comment = 'More crashes incoming. Current crash count is at `{0}`.'.format(count)
        return new_comment

    def __init__(self):
        if is_appengine_local():
            secrets = DEBUG_CLIENT_SECRETS
        else:
            secrets = CLIENT_SECRETS
        with open(secrets, 'r') as contents:
            secrets = json.loads(contents.read())
            github_token = secrets.get(TOKEN_KEY)
            self.webhook_secret = secrets.get(WEBHOOK_SECRET)
            if is_appengine_local():
                self.reporter_host = DEBUG_CRASH_REPORTER_HOST
                self.repo_name = '{0}/{1}'.format(DEBUG_OWNER, DEBUG_REPO)
            else:
                self.reporter_host = CRASH_REPORTER_HOST
                self.repo_name = '{0}/{1}'.format(OWNER, REPO)
            self.github_client = Github(login_or_token=github_token)

    def create_issue(self, crash_report):
        """
        Submits a GitHub issue for a given fingerprint.
        """
        # get repository
        repository = self.github_client.get_repo(self.repo_name)
        # create issue
        issue = repository.create_issue(
            title=GithubClient.issue_title(crash_report),
            body=self.issue_body(crash_report),
            labels=['crash reporter']
        )
        return issue

    def create_comment(self, crash_report):
        """
        Updates a crash report with the comment.
        """
        count = CrashReport.get_count(crash_report.name)
        issue_number = int(crash_report.issue)
        comment_body = self.issue_comment(count)

        # get repo
        repository = self.github_client.get_repo(self.repo_name)
        issue = repository.get_issue(issue_number)
        # create comment
        comment = issue.create_comment(comment_body)
        return {
            'issue': issue,
            'comment': comment
        }
