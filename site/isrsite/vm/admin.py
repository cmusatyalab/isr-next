from django.contrib import admin
from .models import *


class IsrUserAdmin(admin.ModelAdmin):
    list_display = ('user', 'secret_key')


class BaseVMAdmin(admin.ModelAdmin):
    list_display = ('uuid', 'name', 'disk_size', 'memory_size')


class VMAdmin(admin.ModelAdmin):
    list_display = ('uuid', 'name', 'lock', 'basevm', 'disk_size',
            'memory_size', 'user', 'current_version',
            'uncommitted_changes', 'date_created', 'comment', 'num_uploaded')


class VersionAdmin(admin.ModelAdmin):
    list_display = ('vm', 'disk_size', 'memory_size', 'number', 'date_created', 'comment')


class LockAdmin(admin.ModelAdmin):
    list_display = ('owner', 'key', 'date_created')


admin.site.register(IsrUser, IsrUserAdmin)
admin.site.register(BaseVM, BaseVMAdmin)
admin.site.register(VM, VMAdmin)
admin.site.register(Version, VersionAdmin)
admin.site.register(Lock, LockAdmin)
