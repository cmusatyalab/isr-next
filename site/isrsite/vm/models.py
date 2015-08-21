from django.db import models
from django.contrib.auth.models import User
import uuid

class IsrUser(models.Model):
    user = models.ForeignKey(User)
    secret_key = models.CharField(max_length=40)

    @property
    def name(self):
        return self.user.username

    def __unicode__(self):
        return self.user.username

class BaseVM(models.Model):
    uuid = models.CharField(max_length=36, primary_key=True,
            default=str(uuid.uuid4()))
    name = models.CharField(max_length=50)
    disk_size = models.PositiveIntegerField()
    memory_size = models.PositiveIntegerField()

    def as_json(self):
        return {
            'uuid': self.uuid,
            'Name': self.name,
            'Disk size': self.disk_size,
            'Memory size': self.memory_size,
        }

    def __unicode__(self):
        return self.name

def _make_uuid():
    return str(uuid.uuid4())

class VM(models.Model):
    basevm = models.ForeignKey('BaseVM')
    uuid = models.CharField(max_length=36, primary_key=True,
            default=_make_uuid)
    user = models.ForeignKey('IsrUser')
    name = models.CharField(max_length=50, default=basevm.name)
    lock = models.ForeignKey('Lock', blank=True, null=True)
    disk_size = models.PositiveIntegerField(default=0)
    memory_size = models.PositiveIntegerField(default=0)
    current_version = models.PositiveIntegerField(default=1)
    uncommitted_changes = models.BooleanField(default=False)
    date_created = models.DateTimeField(auto_now_add=True)
    comment = models.TextField(max_length=1000)
    num_uploaded = models.PositiveIntegerField(default=0)

    def _datestr(self):
        return self.date_created.strftime('%m-%d-%Y %H:%M:%S %Z')

    def as_json(self):
        return {
            'uuid': self.uuid,
            'Name': self.name,
            'Base vm': self.basevm.name,
            'Version': self.current_version,
            'Disk size': self.disk_size,
            'Memory size': self.memory_size,
            'Lock owner': self.lock.owner if self.lock is not None else 'None',
            'Date created': self._datestr(),
        }

    def __unicode__(self):
        return '%s:%s' % (self.user, self.name)

class Version(models.Model):
    vm = models.ForeignKey('VM')
    disk_size = models.PositiveIntegerField()
    memory_size = models.PositiveIntegerField()
    number = models.PositiveIntegerField()
    date_created = models.DateTimeField(auto_now_add=True)
    comment = models.TextField(max_length=1000)

    def _datestr(self):
        return self.date_created.strftime('%m-%d-%Y %H:%M:%S %Z')

    def as_json(self):
        return {
            '#': self.number,
            'Date created': self._datestr(),
            'Comment': self.comment,
        }

    def __unicode__(self):
        return '%s: %s: %s' % (self.vm.user, self.vm.name, self.number)

class Lock(models.Model):
    owner = models.CharField(max_length=36, default=_make_uuid())
    key = models.CharField(max_length=36, default=_make_uuid())
    date_created = models.DateTimeField(auto_now_add=True)

    def __unicode__(self):
        return self.owner
