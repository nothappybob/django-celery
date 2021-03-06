from datetime import datetime
from time import time

from anyjson import deserialize
from django.db import transaction

from celery.beat import Scheduler, ScheduleEntry

from djcelery.models import PeriodicTask, PeriodicTasks


class ModelEntry(ScheduleEntry):
    _save_fields = ["last_run_at", "total_run_count", "no_changes"]

    def __init__(self, model):
        self.name = model.name
        self.task = model.task
        self.schedule = model.schedule
        self.args = deserialize(model.args)
        self.kwargs = deserialize(model.kwargs)
        self.options = {"queue": model.queue,
                        "exchange": model.exchange,
                        "routing_key": model.routing_key,
                        "expires": model.expires}
        self.total_run_count = model.total_run_count
        self.model = model

        if not model.last_run_at:
            model.last_run_at = datetime.now()
        self.last_run_at = model.last_run_at

    def next(self):
        self.model.last_run_at = datetime.now()
        self.model.total_run_count += 1
        self.model.no_changes = True
        return self.__class__(self.model)

    def save(self):
        # Object may not be synchronized, so only
        # change the fields we care about.
        obj = self.model._default_manager.get(pk=self.model.pk)
        for field in self._save_fields:
            setattr(obj, field, getattr(self.model, field))
        obj.save()


class DatabaseScheduler(Scheduler):
    Entry = ModelEntry
    Model = PeriodicTask
    Changes = PeriodicTasks
    _schedule = None
    _last_timestamp = None

    def __init__(self, *args, **kwargs):
        Scheduler.__init__(self, *args, **kwargs)
        self.max_interval = 5
        self._dirty = set()
        self._last_flush = None
        self._flush_every = 3 * 60

    def setup_schedule(self):
        pass

    def all_as_schedule(self):
        self.logger.debug("DatabaseScheduler: Fetching database schedule")
        return dict((model.name, self.Entry(model))
                        for model in self.Model.objects.enabled())

    def schedule_changed(self):
        if self._last_timestamp is not None:
            ts = self.Changes.last_change()
            if not ts or ts < self._last_timestamp:
                return False

        self._last_timestamp = datetime.now()
        return True

    def should_flush(self):
        return not self._last_flush or \
                    (time() - self._last_flush) > self._flush_every

    def reserve(self, entry):
        new_entry = Scheduler.reserve(self, entry)
        # Need to story entry by name, because the entry may change
        # in the mean time.
        self._dirty.add(new_entry.name)
        if self.should_flush():
            self.logger.debug("Celerybeat: Writing schedule changes...")
            self.flush()
        return new_entry

    @transaction.commit_manually()
    def flush(self):
        if not self._dirty:
            return
        try:
            while self._dirty:
                try:
                    name = self._dirty.pop()
                    self.schedule[name].save()
                except KeyError:
                    continue
        except:
            transaction.rollback()
            raise
        else:
            transaction.commit()
            self._last_flush = time()

    def get_schedule(self):
        if self.schedule_changed():
            self.flush()
            self.logger.debug("DatabaseScheduler: Schedule changed.")
            self._schedule = self.all_as_schedule()
        return self._schedule
