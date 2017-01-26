# -*-  coding: utf-8 -*-
"""
"""

# Copyright (C) 2015 ZetaOps Inc.
#
# This file is licensed under the GNU General Public License v3
# (GPLv3).  See LICENSE.txt for details.

from __future__ import print_function
import codecs
from random import randint
from sys import stdout
import threading
import time
from riak import ConflictError, RiakError
from pyoko.conf import settings
from pyoko.db.connection import client, log_bucket
import os, inspect
import concurrent.futures as con

try:
    from urllib.request import urlopen
    from urllib.error import HTTPError
except ImportError:
    from urllib2 import urlopen, HTTPError


class FakeContext(object):
    def has_permission(self, perm):
        return True


fake_context = FakeContext()


def wait_for_schema_creation(index_name):
    url = 'http://%s:8093/internal_solr/%s/select' % (settings.RIAK_SERVER, index_name)
    print("pinging solr schema: %s" % url, end='')
    while True:
        try:
            urlopen(url)
            return
        except HTTPError as e:
            if e.code == 404:
                time.sleep(1)
                import traceback
                print(traceback.format_exc())
            else:
                raise


def wait_for_schema_deletion(index_name):
    url = 'http://%s:8093/internal_solr/%s/select' % (settings.RIAK_SERVER, index_name)
    i = 0
    while True:
        i += 1
        stdout.write("\r Waiting for actual deletion of solr schema %s %s" % (index_name, i * '.'))
        stdout.flush()
        try:
            urlopen(url)
            time.sleep(1)
        except HTTPError as e:
            print("")
            if e.code == 404:
                return
            else:
                raise


def get_schema_from_solr(index_name):
    url = 'http://%s:8093/internal_solr/%s/admin/file?file=%s.xml' % (settings.RIAK_SERVER,
                                                                      index_name, index_name)
    try:
        return urlopen(url).read()
    except HTTPError as e:
        if e.code == 404:
            return ""
        else:
            raise


class SchemaUpdater(object):
    """
    traverses trough all models, collects fields marked for index or store in solr
    then creates a solr schema for these fields.
    """

    FIELD_TEMPLATE = '<field    type="{type}" name="{name}"  indexed="{index}" ' \
                     'stored="{store}" multiValued="{multi}" />'

    def __init__(self, models, threads, force):
        self.report = []
        self.models = models
        self.force = force
        self.client = client
        self.threads = int(threads)
        self.faulty_models = []
        self.decr_thread_number = int(self.threads / 5) or 1

    def run(self, check_only=False):
        """

        Args:
            check_only:  do not migrate, only report migration is needed or not if True

        Returns:

        """
        models = self.models
        num_models = len(models)
        n_val = self.client.bucket_type(settings.DEFAULT_BUCKET_TYPE).get_property('n_val')
        self.client.create_search_index('foo_index', '_yz_default', n_val=n_val)

        print("Schema creation started for %s model(s) with max %s threads\n" % (
            num_models, self.threads))

        with con.ThreadPoolExecutor(max_workers=self.threads) as executor:
            for model in models:
                ins = model(fake_context)
                fields = self.get_schema_fields(ins._collect_index_fields())
                new_schema = self.compile_schema(fields)
                executor.submit(self.apply_schema, self.client, self.force,
                                new_schema, model, check_only, self.faulty_models)

        if self.faulty_models:
            self.threads -= self.decr_thread_number
            if self.threads <= 0: self.threads = 1
            self.models = self.faulty_models
            self.faulty_models = []
            self.run()

    def create_report(self):
        """
        creates a text report for the human user
        :return: str
        """

        if self.report:
            self.report += "\n Operation took %s secs" % round(
                time.time() - self.t1)
        else:
            self.report = "Operation failed: %s \n" % self.report
        return self.report

    @classmethod
    def get_schema_fields(cls, fields):
        """

        :param list[(,)] fields: field props tupple list
        :rtype: list[str]
        :return: schema fields list
        """
        return [cls.FIELD_TEMPLATE.format(name=name,
                                          type=field_type,
                                          index=str(index).lower(),
                                          store=str(store).lower(),
                                          multi=str(multi).lower())
                for name, field_type, index, store, multi in fields]

    @staticmethod
    def compile_schema(fields):
        """
        joins schema fields with base solr schema

        :param list[str] fields: field list
        :return: compiled schema
        :rtype: byte
        """
        path = os.path.dirname(os.path.realpath(__file__))
        # path = os.path.dirname(
        #     os.path.abspath(inspect.getfile(inspect.currentframe())))
        with codecs.open("%s/solr_schema_template.xml" % path, 'r', 'utf-8') as fh:
            schema_template = fh.read()
        return schema_template.format('\n'.join(fields)).encode('utf-8')

    @staticmethod
    def apply_schema(client, force, new_schema, model, check_only, faulty_models):
        """
        riak doesn't support schema/index updates ( http://git.io/vLOTS )

        as a workaround, we create a temporary index,
        attach it to the bucket, delete the old index/schema,
        re-create the index with new schema, assign it to bucket,
        then delete the temporary index.

        :param byte new_schema: compiled schema
        :param str bucket_name: name of schema, index and bucket.
        :return: True or False
        :rtype: bool
        """
        try:
            bucket_name = model._get_bucket_name()
            bucket_type = client.bucket_type(settings.DEFAULT_BUCKET_TYPE)
            bucket = bucket_type.bucket(bucket_name)
            n_val = bucket_type.get_property('n_val')
            index_name = "%s_%s" % (settings.DEFAULT_BUCKET_TYPE, bucket_name)
            if not force:
                try:
                    schema = get_schema_from_solr(index_name)
                    if schema == new_schema:
                        print("Schema %s is already up to date, nothing to do!" % index_name)
                        return
                    elif check_only and schema != new_schema:
                        print("Schema %s is not up to date, migrate this model!" % index_name)
                        return
                except:
                    import traceback
                    traceback.print_exc()
            bucket.set_property('search_index', 'foo_index')
            try:
                client.delete_search_index(index_name)
            except RiakError as e:
                if 'notfound' != e.value:
                    raise
            wait_for_schema_deletion(index_name)
            client.create_search_schema(index_name, new_schema)
            client.create_search_index(index_name, index_name, n_val)
            bucket.set_property('search_index', index_name)
            print("+ %s (%s)" % (model.__name__, index_name))
            stream = bucket.stream_keys()
            i = 0
            unsaved_keys = []
            for key_list in stream:
                for key in key_list:
                    i += 1
                    # time.sleep(0.4)
                    try:
                        obj = bucket.get(key)
                        if obj.data:
                            obj.store()
                    except ConflictError:
                        unsaved_keys.append(key)
                        print("Error on save. Record in conflict: %s > %s" % (bucket_name, key))
                    except:
                        unsaved_keys.append(key)
                        print("Error on save! %s > %s" % (bucket_name, key))
                        import traceback
                        traceback.print_exc()
            stream.close()
            print("Re-indexed %s records of %s" % (i, bucket_name))
            if unsaved_keys:
                print("\nThese keys cannot be updated:\n\n", unsaved_keys)

        except:
            print("n_val: %s" % n_val)
            print("bucket_name: %s" % bucket_name)
            print("bucket_type: %s" % bucket_type)
            faulty_models.append(model)
