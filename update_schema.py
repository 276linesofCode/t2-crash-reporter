import logging

import webapp2
from google.appengine.ext import db
from google.appengine.ext import deferred

from model import CrashReport
from search_model import Search

BATCH_SIZE = 100


class SchemaUpdater(object):
    """
    Updates the crash reporter schema. Adds the state on the crash reporter.
    """
    @classmethod
    def delete_search_indexes(cls):
        logging.info("Deleting all entries from Search Indexes.")
        Search.delete_all_in_index()

    @classmethod
    def update(cls, cursor=None):
        logging.info('Upgrading schema for Crash Reports (Cursor = %s)' % unicode(cursor))
        query = CrashReport.all()
        if cursor:
            query.with_cursor(cursor)

        crash_reports = list()
        for crash_report in query.fetch(limit=BATCH_SIZE):
            crash_report.version = '2'
            crash_report.state = 'unresolved'
            crash_reports.append(crash_report)

        if crash_reports:
            updated = len(crash_reports)
            logging.info('Updating %s entities', updated)
            # update
            db.put(crash_reports)
            Search.add_crash_reports(crash_reports)
            # schedule next request
            deferred.defer(SchemaUpdater.update, cursor=query.cursor())


class RemoveSearchIndexes(webapp2.RequestHandler):
    def get(self):
        deferred.defer(SchemaUpdater.delete_search_indexes)
        message = 'Removing all search indexes started'
        logging.info(message)
        self.response.out.write(message)


class UpdateSchemaHandler(webapp2.RequestHandler):
    def get(self):
        deferred.defer(SchemaUpdater.update)
        message = 'Schema Updates Started'
        logging.info(message)
        self.response.out.write(message)
