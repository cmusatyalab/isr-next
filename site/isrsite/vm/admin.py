#
# isrsite - ISR server
#
# Copyright (C) 2014-2015 Carnegie Mellon University
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of version 2 of the GNU General Public License as published
# by the Free Software Foundation.  A copy of the GNU General Public License
# should have been distributed along with this program in the file
# COPYING.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
# for more details.
#

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
