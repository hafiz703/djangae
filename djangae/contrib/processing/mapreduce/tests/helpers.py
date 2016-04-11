from django.db import models
from djangae.test import TestCase

from djangae.contrib.processing.mapreduce import map_queryset


class TestModel(models.Model):
    class Meta:
        app_label = "mapreduce"


class Counter(models.Model):
    count = models.PositiveIntegerField(default=0)


def count(instance, counter_id):
    counter = Counter.objects.get(pk=counter_id)
    counter.count = models.F('count') + 1
    counter.save()


def delete():
    TestModel.objects.all().delete()


class MapQuerysetTests(TestCase):
    def setUp(self):
        for i in xrange(5):
            TestModel.objects.create()

    def test_mapping_over_queryset(self):
        counter = Counter.objects.create()

        map_queryset(
            TestModel.objects.all(),
            count,
            finalize_func=delete,
            counter_id=counter.pk
        )

        self.process_task_queues()
        counter.refresh_from_db()

        self.assertEqual(5, counter.count)
        self.assertFalse(TestModel.objects.count())

    def test_filters_apply(self):
        pass