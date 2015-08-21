from django.conf.urls import patterns, url
from .views import *
from django.views.generic.base import RedirectView

urlpatterns = patterns('',
    url(r'^info$', vm_info, name='vm_info'),
    url(r'^base_info$', base_vm_info, name='base_vm_info'),
    url(r'^checkout/(?P<uuid>[-\w]+)', checkout, name='checkout'),
    url(r'^create', create_vm_from_base, name='create'),
    url(r'^(?P<uuid>[-\w]+)/update', update, name='update'),
    url(r'^(?P<uuid>[-\w]+)/(?P<current>\d+)/(?P<new>\d+)$', validate, name='validate'),
    url(r'^(?P<uuid>[-\w]+)/(?P<version>\d+)$', vm, name='vm'),
    url(r'^(?P<uuid>[-\w]+)/(?P<version>\d+)/(?P<image>[-\w]+)/size$', size, name='size'),
    url(r'^(?P<uuid>[-\w]+)/(?P<version>\d+)/(?P<image>[-\w]+)/chunk/(?P<num>\d+)/$', chunk, name='chunk'),
    url(r'^version/(?P<uuid>[-\w]+)', version, name='version'),
    url(r'^commit/(?P<uuid>[-\w]+)', commit, name='commit'),
    url(r'^comment/(?P<uuid>[-\w]+)', comment, name='comment'),
    url(r'^discard/(?P<uuid>[-\w]+)', discard, name='discard'),
)

