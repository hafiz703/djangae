import datetime
import pipeline
import logging
from mapreduce import context
from mapreduce import pipeline_base

from django.conf import settings
from django.apps import apps
from django.db import models, connections
from django.dispatch import receiver
from django.db.models.signals import post_save

from google.appengine.api import datastore
from google.appengine.ext import deferred

from djangae.db import transaction
from djangae.fields import RelatedSetField
from djangae.db.utils import django_instance_to_entity
from djangae.db.unique_utils import unique_identifiers_from_entity
from djangae.db.constraints import UniqueMarker
from djangae.db.caching import disable_cache
from mapreduce.mapper_pipeline import MapperPipeline

ACTION_TYPES = [
    ('check', 'Check'),  # Verify all models unique contraint markers exist and are assigned to it.
    ('repair', 'Repair'),  # Recreate any missing markers
    ('clean', 'Clean'),  # Remove any marker that isn't properly linked to an instance.
]

ACTION_STATUSES = [
    ('running', 'Running'),
    ('done', 'Done'),
]

LOG_MSGS = [
    ('missing_marker', "Marker for the unique constraint is missing"),
    ('missing_instance', "Unique constraint marker exists, but doesn't point to the instance"),
    ('already_assigned', "Marker is assigned to a different instance already"),
    ('old_instance_key', "Marker was created when instance was a StringProperty")
]

MAX_ERRORS = 100


def encode_model(model):
    return "%s,%s" % (model._meta.app_label, model._meta.model_name)


def decode_model(model_str):
    return apps.get_model(*model_str.split(','))


class ActionLog(models.Model):
    instance_key = models.TextField()
    marker_key = models.CharField(max_length=500)
    log_type = models.CharField(max_length=255, choices=LOG_MSGS)
    action = models.ForeignKey('UniqueAction')


class UniqueAction(models.Model):
    action_type = models.CharField(choices=ACTION_TYPES, max_length=100)
    model = models.CharField(max_length=100)
    db = models.CharField(max_length=100, default='default')
    status = models.CharField(choices=ACTION_STATUSES, default=ACTION_STATUSES[0][0], editable=False, max_length=100)
    logs = RelatedSetField(ActionLog, editable=False)

def _log_action(action_id, log_type, instance_key, marker_key):
    @transaction.atomic(xg=True)
    def _atomic(action_id, log_type, instance_key, marker_key):
        action = UniqueAction.objects.get(pk=action_id)
        if len(action.logs) > MAX_ERRORS:
            return

        log = ActionLog.objects.create(
            action_id=action_id,
            log_type=log_type,
            instance_key=instance_key,
            marker_key=marker_key)
        action.logs.add(log)
        action.save()
    _atomic(action_id, log_type, instance_key, marker_key)


def log(action_id, log_type, instance_key, marker_key, defer=True):
    """ Shorthand for creating an ActionLog.

    Defer doesn't accept an inline function or an atomic wrapped function directly, so
    we defer a helper function, which wraps the transactionaly decorated one. """
    if defer:
        deferred.defer(_log_action, action_id, log_type, instance_key, marker_key)
    else:
        _log_action(action_id, log_type, instance_key, marker_key)


@receiver(post_save, sender=UniqueAction)
def start_action(sender, instance, created, raw, **kwargs):
    if created == False:
        # we are saving because status is now "done"?
        return

    kwargs = dict(
        action_pk=instance.pk,
    )

    if instance.action_type == "clean":
        kwargs.update(model=instance.model)
        pipe = CleanMapper(model=instance.model, db=instance.db, action_pk=instance.pk)
    else:
        pipe = CheckRepairMapper(model=instance.model, db=instance.db, action_pk=instance.pk, repair=instance.action_type=="repair")
    pipe.start()


def _finish(action_pk):
    @transaction.atomic
    def finish_the_action():
        action = UniqueAction.objects.get(pk=action_pk)
        action.status = "done"
        action.save()
    finish_the_action()


def check_repair_map(instance, *args, **kwargs):
    """ Figure out what markers the instance should use and verify they're attached to
    this instance. Log any weirdness and in repair mode - recreate missing markers. """
    params = context.get().mapreduce_spec.mapper.params
    action_id = params.get("action_pk")
    repair = params.get("repair")
    alias = params.get("db", "default")
    namespace = settings.DATABASES.get(alias, {}).get("NAMESPACE")
    assert alias == (instance._state.db or "default")
    entity = django_instance_to_entity(connections[alias], type(instance), instance._meta.fields, raw=True, instance=instance, check_null=False)
    identifiers = unique_identifiers_from_entity(type(instance), entity, ignore_pk=True)
    identifier_keys = [datastore.Key.from_path(UniqueMarker.kind(), i, namespace=namespace) for i in identifiers]

    markers = datastore.Get(identifier_keys)
    instance_key = str(entity.key())

    markers_to_save = []
    for i, m in zip(identifier_keys, markers):
        marker_key = str(i)
        if m is None:
            # Missig marker
            if repair:
                new_marker = datastore.Entity(UniqueMarker.kind(), name=i.name(), namespace=namespace)
                new_marker['instance'] = entity.key()
                new_marker['created'] = datetime.datetime.now()
                markers_to_save.append(new_marker)
            else:
                log(action_id, "missing_marker", instance_key, marker_key, defer=False)

        elif 'instance' not in m or not m['instance']:
            # Marker with missining instance attribute
            if repair:
                m['instance'] = entity.key()
                markers_to_save.append(m)
            else:
                log(action_id, "missing_instance", instance_key, marker_key, defer=False)

        elif m['instance'] != entity.key():

            if isinstance(m['instance'], basestring):
                m['instance'] = datastore.Key(m['instance'])

                if repair:
                    markers_to_save.append(m)
                else:
                    log(action_id, "old_instance_key", instance_key, marker_key, defer=False)

            if m['instance'] != entity.key():
                # Marker already assigned to a different instance
                log(action_id, "already_assigned", instance_key, marker_key, defer=False)
                # Also log in repair mode as reparing would break the other instance.

    if markers_to_save:
        datastore.Put(markers_to_save)


class CallbackPipeline(pipeline_base.PipelineBase):

    def run(self, *args, **kwargs):
        _finish(*args, **kwargs)


class CheckRepairMapper(pipeline_base.PipelineBase):

    def run(self, *args, **kwargs):
        mapper_params = {}
        mapper_params['input_reader'] = kwargs
        mapper_params['action_pk'] = kwargs['action_pk']
        mapper_params['db'] = kwargs['db']
        mapper_params['repair'] = kwargs['repair']
        yield MapperPipeline(
            "check-repair",
            "djangae.contrib.uniquetool.models.check_repair_map",
            "djangae.contrib.processing.mapreduce.input_readers.DjangoInputReader",
            params=mapper_params,
            shards=10
        )

        yield CallbackPipeline(kwargs['action_pk'])


def clean_map(entity, *args, **kwargs):
    """ The Clean mapper maps over all UniqueMarker instances. """
    params = context.get().mapreduce_spec.mapper.params
    model = params['model']

    alias = params.get("namespace", "default")
    namespace = settings.DATABASES.get(alias, {}).get("NAMESPACE", "")

    model = decode_model(model)
    if not entity.key().id_or_name().startswith(model._meta.db_table + "|"):
        # Only include markers which are for this model
        return

    assert namespace == entity.namespace()
    with disable_cache():
        # At this point, the entity is a unique marker that is linked to an instance of 'model', now we should see if that instance exists!
        instance_id = entity["instance"].id_or_name()
        try:
            instance = model.objects.using(alias).get(pk=instance_id)
        except model.DoesNotExist:
            logging.info("Deleting unique marker {} because the associated instance no longer exists".format(entity.key().id_or_name()))
            datastore.Delete(entity)
            return

        # Get the possible unique markers for the entity, if this one doesn't exist in that list then delete it
        instance_entity = django_instance_to_entity(connections[alias], model, instance._meta.fields, raw=True, instance=instance, check_null=False)
        identifiers = unique_identifiers_from_entity(model, instance_entity, ignore_pk=True)
        identifier_keys = [datastore.Key.from_path(UniqueMarker.kind(), i, namespace=entity["instance"].namespace()) for i in identifiers]
        if entity.key() not in identifier_keys:
            logging.info("Deleting unique marker {} because the it no longer represents the associated instance state".format(entity.key().id_or_name()))
            datastore.Delete(entity)


class CleanMapper(pipeline_base.PipelineBase):

    def run(self, *args, **kwargs):
        mapper_params = {}
        mapper_params['input_reader'] = {
            'entity_kind': '_djangae_unique_marker',
            'keys_only': False,
            'kwargs': kwargs,
            'args': args,
            'namespace': settings.DATABASES.get(kwargs['db'], {}).get('NAMESPACE', ''),
        }
        mapper_params['action_pk'] = kwargs['action_pk']
        mapper_params['namespace'] = kwargs['db']
        mapper_params['model'] = kwargs['model']

        yield MapperPipeline(
            "Repair unique markers on {}".format(kwargs["model"]),
            handler_spec="djangae.contrib.uniquetool.models.clean_map",
            input_reader_spec="mapreduce.input_readers.RawDatastoreInputReader",
            params=mapper_params,
            shards=10
        )
        
        yield CallbackPipeline(kwargs['action_pk'])
