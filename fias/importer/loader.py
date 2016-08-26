# coding: utf-8
from __future__ import unicode_literals, absolute_import

import datetime
from django.conf import settings
from django import db
from django.db import IntegrityError
from progress.helpers import WritelnMixin
from sys import stderr

from fias.importer.signals import pre_import_table, post_import_table
from fias.importer.validators import validators


class LoadingBar(WritelnMixin):
    file = stderr

    text = 'Table: %(table)s.' \
           ' Load: %(loaded)d | Upd: %(updated)d | Skip:  %(skipped)d | Regr: %(depth)d[%(stack_str)s]' \
           ' \tFilename: %(filename)s'

    loaded = 0
    updated = 0
    skipped = 0
    depth = 0
    stack = []
    stack_str = 0
    hide_cursor = False

    def __init__(self, message=None, **kwargs):
        self.table = kwargs.pop('table', 'unknown')
        self.filename = kwargs.pop('filename', 'unknown')
        super(LoadingBar, self).__init__(message=message, **kwargs)

    def __getitem__(self, key):
        if key.startswith('_'):
            return None
        return getattr(self, key, None)

    def update(self, loaded=0, updated=0, skipped=0, regress_depth=0, regress_len=0):
        if loaded:
            self.loaded = loaded
        if updated:
            self.updated = updated
        if skipped:
            self.skipped = skipped

        self.depth = regress_depth
        if not self.depth:
            self.stack_str = 0
        else:
            regress_len = str(regress_len)
            stack_len = len(self.stack)
            if stack_len == self.depth:
                self.stack[self.depth-1] = regress_len
            elif stack_len < self.depth:
                self.stack.append(regress_len)
            else:
                self.stack = self.stack[0:self.depth]
                self.stack[self.depth-1] = regress_len

            self.stack_str = '/'.join(self.stack)

        ln = self.text % self
        self.writeln(ln)


class TableLoader(object):

    def __init__(self, limit=10000):
        self.limit = int(limit)
        self.counter = 0
        self.upd_counter = 0
        self.skip_counter = 0
        self.today = datetime.date.today()

    def validate(self, table, item):
        if item is None or item.pk is None:
            return False

        return validators.get(table.name, lambda x, **kwargs: True)(item, today=self.today)

    def regressive_create(self, table, objects, bar, depth=1):
        count = len(objects)
        batch_len = count // 3 or 1
        objects = list(objects)

        for i in range(0, batch_len):
            batch = objects[i*batch_len:(i+1)*batch_len]
            bar.update(regress_depth=depth, regress_len=batch_len)
            try:
                table.model.objects.bulk_create(batch)
            except IntegrityError:
                if batch_len <= 1:
                    self.counter -= 1
                    self.skip_counter += 1
                    bar.update(skipped=self.skip_counter)
                    continue
                else:
                    self.regressive_create(table, batch, bar=bar, depth=depth+1)

    def create(self, table, objects, bar):
        try:
            table.model.objects.bulk_create(objects)
        except IntegrityError as e:
            self.regressive_create(table, objects, bar)

        #  Обнуляем индикатор регрессии
        bar.update(regress_depth=0, regress_len=0)
        if settings.DEBUG:
            db.reset_queries()

    def load(self, tablelist, table):
        pre_import_table.send(sender=self.__class__, table=table)
        self.do_load(tablelist=tablelist, table=table)
        post_import_table.send(sender=self.__class__, table=table)

    def do_load(self, tablelist, table):
        bar = LoadingBar(table=table.name, filename=table.filename)

        objects = set()
        for item in table.rows(tablelist=tablelist):
            if not self.validate(table, item):
                self.skip_counter += 1
                continue

            objects.add(item)
            self.counter += 1

            if self.counter and self.counter % self.limit == 0:
                self.create(table, objects, bar=bar)
                objects.clear()
                bar.update(loaded=self.counter)

        if objects:
            self.create(table, objects, bar=bar)
            bar.update(loaded=self.counter)

        bar.update(loaded=self.counter, skipped=self.skip_counter)
        bar.finish()


class TableUpdater(TableLoader):

    def __init__(self, limit=10000):
        self.upd_limit = 100
        super(TableUpdater, self).__init__(limit=limit)

    def do_load(self, tablelist, table):
        bar = LoadingBar(table=table.name, filename=table.filename)

        model = table.model
        objects = set()
        for item in table.rows(tablelist=tablelist):
            if not self.validate(table, item):
                self.skip_counter += 1
                continue

            try:
                old_obj = model.objects.get(pk=item.pk)
            except model.DoesNotExist:
                objects.add(item)
                self.counter += 1
            else:
                if not hasattr(item, 'updatedate') or old_obj.updatedate < item.updatedate:
                    item.save()
                    self.upd_counter += 1

            if self.counter and self.counter % self.limit == 0:
                self.create(table, objects)
                objects.clear()
                bar.update(loaded=self.counter)

            if self.upd_counter and self.upd_counter % self.upd_limit == 0:
                bar.update(updated=self.upd_counter)

        if objects:
            self.create(table, objects)
            bar.update(loaded=self.counter)

        bar.update(loaded=self.counter, updated=self.upd_counter, skipped=self.skip_counter)
        bar.finish()
