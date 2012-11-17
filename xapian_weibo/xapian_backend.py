#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys
import xapian
import cPickle as pickle
import simplejson as json
import pymongo
import scws
import datetime
import calendar
from argparse import ArgumentParser


SCWS_ENCODING = 'utf-8'
SCWS_RULES = '/usr/local/scws/etc/rules.utf8.ini'
CHS_DICT_PATH = '/usr/local/scws/etc/dict.utf8.xdb'
CHT_DICT_PATH = '/usr/local/scws/etc/dict_cht.utf8.xdb'
CUSTOM_DICT_PATH = '../dict/userdic.txt'
IGNORE_PUNCTUATION = 1
EXTRA_STOPWORD_PATH = '../dict/stopword.dic'
EXTRA_EMOTIONWORD_PATH = '../dict/emotionlist.txt'
PROCESS_IDX_SIZE = 100000

SCHEMA_VERSION = 1
DOCUMENT_ID_TERM_PREFIX = 'M'
DOCUMENT_CUSTOM_TERM_PREFIX = 'X'


class XapianBackend(object):
    def __init__(self, dbpath, schema_version):
        self.path = dbpath
        if schema_version == 1:
            self.schema = Schema.v1

        self.databases = {}
        self.load_scws()
        self.load_mongod()
        self.load_extra_dic()

    def document_count(self, folder):
        try:
            return _database(folder).get_doccount()
        except InvalidIndexError:
            return 0

    def generate(self, start_time=None):
        folders_with_date = []

        if not debug and start_time:
            start_time = datetime.datetime.strptime(start_time, '%Y-%m-%d')
            folder = "_%s_%s" % (self.path, start_time.strftime('%Y-%m-%d'))
            folders_with_date.append((start_time, folder))
        elif debug:
            start_time = datetime.datetime(2009, 8, 1)
            step_time = datetime.timedelta(days=50)
            while start_time < datetime.datetime.today():
                folder = "_%s_%s" % (self.path, start_time.strftime('%Y-%m-%d'))
                folders_with_date.append((start_time, folder))
                start_time += step_time

        self.folders_with_date = folders_with_date

    def load_extra_dic(self):
        self.emotion_words = [line.strip('\r\n') for line in file(EXTRA_EMOTIONWORD_PATH)]

    def load_scws(self):
        s = scws.Scws()
        s.set_charset(SCWS_ENCODING)

        s.set_dict(CHS_DICT_PATH, scws.XDICT_MEM)
        s.add_dict(CHT_DICT_PATH, scws.XDICT_MEM)
        s.add_dict(CUSTOM_DICT_PATH, scws.XDICT_TXT)

        # 把停用词全部拆成单字，再过滤掉单字，以达到去除停用词的目的
        s.add_dict(EXTRA_STOPWORD_PATH, scws.XDICT_TXT)
        # 即基于表情表对表情进行分词，必要的时候在返回结果处或后剔除
        s.add_dict(EXTRA_EMOTIONWORD_PATH, scws.XDICT_TXT)

        s.set_rules(SCWS_RULES)
        s.set_ignore(IGNORE_PUNCTUATION)
        self.s = s

    def load_mongod(self):
        connection = pymongo.Connection()
        db = connection.admin
        db.authenticate('root', 'root')
        db = connection.weibo
        self.db = db

    def get_database(self, folder):
        if folder not in self.databases:
            self.databases[folder] = _database(folder, writable=True)
        return self.databases[folder]

    #@profile
    def load_and_index_weibos(self, start_time=None):
        if not debug and start_time:
            start_time = self.folders_with_date[0][0]
            end_time = start_time + datetime.timedelta(days=50)
            weibos = self.db.statuses.find({
                self.schema['posted_at_key']: {
                    '$gte': calendar.timegm(start_time.timetuple()),
                    '$lt': calendar.timegm(end_time.timetuple())
                }
            })
            print 'prod mode: loaded weibos from mongod'
        elif debug:
            with open("../test/sample_tweets.js") as f:
                weibos = json.loads(f.readline())
            print 'debug mode: loaded weibos from file'

        count = 0
        try:
            for weibo in weibos:
                count += 1
                posted_at = datetime.datetime.fromtimestamp(weibo[self.schema['posted_at_key']])
                if not debug and start_time:
                    folder = self.folders_with_date[0][1]
                elif debug:
                    for i in xrange(len(self.folders_with_date) - 1):
                        if self.folders_with_date[i][0] <= posted_at < self.folders_with_date[i + 1][0]:
                            folder = self.folders_with_date[i][1]
                            break
                    else:
                        if posted_at >= self.folders_with_date[i + 1][0]:
                            folder = self.folders_with_date[i + 1][1]

                self.update(folder, weibo)
                if count % PROCESS_IDX_SIZE == 0:
                    print '[%s] folder[%s] num indexed: %s' % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), folder, count)
        except Exception:
            raise

        finally:
            for database in self.databases.itervalues():
                database.close()

            for _, folder in self.folders_with_date:
                print 'index size', folder, self.document_count(folder)

    def update(self, folder, weibo):
        document = xapian.Document()
        document_id = DOCUMENT_ID_TERM_PREFIX + weibo[self.schema['obj_id']]
        for field in self.schema['idx_fields']:
            self.index_field(field, document, weibo, SCHEMA_VERSION)

        document.set_data(pickle.dumps(
            weibo, pickle.HIGHEST_PROTOCOL
        ))
        document.add_term(document_id)
        self.get_database(folder).replace_document(document_id, document)

    def index_field(self, field, document, weibo, schema_version):
        prefix = DOCUMENT_CUSTOM_TERM_PREFIX + field['field_name'].upper()
        if schema_version == 1:
            if field['field_name'] in ['uid', 'name']:
                term = _marshal_term(weibo[field['field_name']])
                document.add_term(prefix + term)
            elif field['field_name'] == 'ts':
                document.add_value(field['column'], _marshal_value(weibo[field['field_name']]))
            elif field['field_name'] == 'text':
                tokens = [token[0] for token
                          in self.s.participle(weibo[field['field_name']].encode('utf-8'))
                          if len(token[0]) > 1]
                for token in tokens:
                    document.add_term(prefix + token)

                document.add_value(field['column'], weibo[field['field_name']])

    def search(self):
        pass


class InvalidIndexError(Exception):
    """Raised when an index can not be opened."""
    pass


class Schema:
    v1 = {
        'obj_id': '_id',
        'posted_at_key': 'ts',
        'idx_fields': [
            {'field_name': 'uid', 'column': 0},
            {'field_name': 'name', 'column': 1},
            {'field_name': 'text', 'column': 2},
            {'field_name': 'ts', 'column': 3}
        ],
    }


def _database(folder, writable=False):
    """
    Private method that returns a xapian.Database for use.

    Optional arguments:
        ``writable`` -- Open the database in read/write mode (default=False)

    Returns an instance of a xapian.Database or xapian.WritableDatabase
    """
    if writable:
        if debug:
            database = xapian.WritableDatabase(folder, xapian.DB_CREATE_OR_OVERWRITE)
        else:
            database = xapian.WritableDatabase(folder, xapian.DB_CREATE_OR_OPEN)
    else:
        try:
            database = xapian.Database(folder)
        except xapian.DatabaseOpeningError:
            raise InvalidIndexError(u'Unable to open index at %s' % folder)

    return database


def _marshal_value(value):
    """
    Private utility method that converts Python values to a string for Xapian values.
    """
    if isinstance(value, (int, long)):
        value = xapian.sortable_serialise(value)
    return value


def _marshal_term(term):
    """
    Private utility method that converts Python terms to a string for Xapian terms.
    """
    if isinstance(term, int):
        term = str(term)
    return term


if __name__ == "__main__":
    """
    cd to test/ folder
    then run 'py (-m memory_profiler) ../xapian_weibo/xapian_backend.py -d hehe'
    http://pypi.python.org/pypi/memory_profiler
    """
    parser = ArgumentParser()
    parser.add_argument('-d', '--debug', action='store_true', help='DEBUG')
    parser.add_argument('-p', '--print_folders', action='store_true', help='PRINT FOLDER THEN EXIT')
    parser.add_argument('-s', '--start_time', nargs=1, help='DATETIME')
    parser.add_argument('dbpath', help='PATH_TO_DATABASE')
    args = parser.parse_args(sys.argv[1:])
    debug = args.debug
    dbpath = args.dbpath

    if args.print_folders:
        debug = True
        xapian_backend = XapianBackend(dbpath, SCHEMA_VERSION)
        xapian_backend.generate()
        for _, folder in xapian_backend.folders_with_date:
            print folder

        sys.exit(0)

    start_time = args.start_time[0] if args.start_time else None
    if debug:
        print 'debug mode(warning): start_time will not be used'
        PROCESS_IDX_SIZE = 10000

    xapian_backend = XapianBackend(dbpath, SCHEMA_VERSION)
    xapian_backend.generate(start_time)
    xapian_backend.load_and_index_weibos(start_time)
