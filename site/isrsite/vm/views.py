# TODO: copyright

from collections import OrderedDict
import json
from urlparse import urljoin
from django.http import (HttpResponse, HttpResponseForbidden,
        HttpResponseBadRequest, Http404, HttpResponseNotFound)
from django.conf import settings
from django.shortcuts import get_object_or_404
import os
import shutil
from django.views.decorators.http import require_http_methods
from .models import *

# Gets a user by looking up the X-Secret-Key from the header
def _get_user(request):
    secret_key = request.META.get('HTTP_X_SECRET_KEY')
    if secret_key is None:
        return HttpResponseBadRequest('Missing secret key')
    return get_object_or_404(IsrUser, secret_key=secret_key)

# Returns a serialized list of chunks that has changed between the current
# version to the new version.
@require_http_methods(['GET',])
def validate(request, uuid, current, new):
    user = _get_user(request)
    vm = get_object_or_404(VM, uuid=uuid)

    if int(current) > vm.current_version or int(new) > vm.current_version:
        return HttpResponseNotFound('Version number exceeds max version')

    chunk_list = {'disk': [], 'memory': []}

    if current == new:
        return HttpResponse(json.dumps(chunk_list),
                content_type='application/json')

    vm_dir = _get_vm_dir(user.name, vm.uuid)
    cur_version_dir = os.path.join(vm_dir, current)
    new_version_dir = os.path.join(vm_dir, new)

    cur_version = get_object_or_404(Version, vm=vm, number=current)
    new_version = get_object_or_404(Version, vm=vm, number=new)

    for image in 'disk', 'memory':
        cur_size = getattr(cur_version, '%s_size' % image)
        new_size = getattr(new_version, '%s_size' % image)

        if new_size < cur_size:
            chunk = (new_size / settings.CHUNK_SIZE) + 1
            last_chunk = cur_size / settings.CHUNK_SIZE

            while chunk <= last_chunk:
                chunk_list[image].append(chunk)
                chunk += 1

        # Find chunks that exist between the lower version and the higher one
        low_number = min(cur_version.number, new_version.number)
        high_number = max(cur_version.number, new_version.number)

        # Iterate through all the chunks rather than versions because its is
        # okay to break out of the loop when the first version which modifies a
        # chunk is found.
        num_chunks = (new_size + settings.CHUNK_SIZE - 1) / settings.CHUNK_SIZE
        chunk = 0
        while chunk < num_chunks:
            ver = low_number + 1
            chunk_dir = _get_chunk_dir(chunk)
            while ver <= high_number:
                chunk_path = os.path.join(vm_dir, str(ver), image,
                        str(chunk_dir), str(chunk))
                if os.path.isfile(chunk_path):
                    chunk_list[image].append(chunk)
                    break
                ver += 1
            chunk += 1
    return HttpResponse(json.dumps(chunk_list), content_type='application/json')

@require_http_methods(['GET',])
def vm_info(request):
    user = _get_user(request)
    vms = VM.objects.filter(user=user).order_by('date_created')

    data = []
    for vm in vms:
        data.append((vm.uuid, vm.as_json()))
        # data[vm.uuid] = vm.as_json()
    return HttpResponse(json.dumps(OrderedDict(data)),
        content_type='application/json')

@require_http_methods(['GET',])
def base_vm_info(request):
    user = _get_user(request)
    # No basevm permissions right now
    base_vms = BaseVM.objects.all()

    data = OrderedDict()
    for base_vm in base_vms:
        data[base_vm.uuid] = base_vm.as_json()
    return HttpResponse(json.dumps(data), content_type='application/json')

@require_http_methods(['POST',])
def update(request, uuid):
    user = _get_user(request)
    vm = get_object_or_404(VM, uuid=uuid)

    for field in request.POST:
        setattr(vm, field, request.POST[field])
    vm.save()
    data = vm.as_json()
    return HttpResponse(json.dumps(data), content_type='application/json')

def _get_chunk_dir(num):
    return int(num) / settings.CHUNKS_PER_DIR * settings.CHUNKS_PER_DIR

def _get_user_dir(name):
    return os.path.join(settings.STORAGE_DIR, 'vm/%s/' % name)

def _get_staging_dir(name, uuid, version):
    return os.path.join(_get_user_dir(name), '%s/stage/%s' % (uuid, version))

def _setup_staging_dir(staging_dir):
    if not os.path.exists(staging_dir):
        os.makedirs(staging_dir)
        os.makedirs(os.path.join(staging_dir, 'disk'))
        os.makedirs(os.path.join(staging_dir, 'memory'))

''' Get root vm dir with all version dirs '''
def _get_vm_dir(name, uuid):
    return os.path.join(_get_user_dir(name), '%s/' % uuid)

''' Given an original vm and version, copy the chunks of that version to the
    directory belonging to a another (could be the same one) vm. '''
def _setup_vm_dir_from_vm(user, vm, version, new_vm, new_version):
    # Create new directories
    vm_dir = _get_vm_dir(user.name, vm.uuid)
    version_path = os.path.join(vm_dir, str(version))
    new_vm_dir = _get_vm_dir(user.name, new_vm.uuid)
    new_version_path = os.path.join(new_vm_dir, str(new_version))
    os.makedirs(new_version_path)
    # Gone?
    shutil.copy2(os.path.join(version_path, 'domain.xml'),
        new_version_path)

    # Iterate through every single chunk and get the latest version (Expensive)
    for image in 'disk', 'memory':
        size_file_path = os.path.join(version_path, image, 'size')

        with open(size_file_path, 'r') as file:
            size = int(file.readline())
        num_chunks = (size + settings.CHUNK_SIZE - 1) / settings.CHUNK_SIZE

        # Set up image directory
        image_dir_path = os.path.join(new_version_path, image)
        os.makedirs(image_dir_path)
        shutil.copy2(size_file_path, image_dir_path)

        chunk = 0
        while chunk < num_chunks:
            chunk_dir = _get_chunk_dir(chunk)
            os.makedirs(os.path.join(image_dir_path, str(chunk_dir)))
            chunk += settings.CHUNKS_PER_DIR

        for chunk in range(num_chunks):
            # Iterate through versions to find latest version of chunk
            curr = int(version)
            dir = _get_chunk_dir(chunk)
            while curr > 0:
                chunk_path = os.path.join(vm_dir,
                        '%s/%s/%s/%s' % (curr, image, dir, chunk))
                if os.path.isfile(chunk_path):
                    break
                curr -= 1
            if curr == 0:
                return HttpResponseNotFound('Chunk not found')
            else:
                # Create symlink to chunk in new version
                new_chunk_path = os.path.join(new_vm_dir,
                        '%s/%s/%s/%s' % (new_version_path, image, dir, chunk))
                os.symlink(chunk_path, new_chunk_path)


@require_http_methods(['GET', 'POST',])
def vm(request, uuid, version):
    user = _get_user(request)
    vm = get_object_or_404(VM, uuid=uuid)
    vm_dir = _get_vm_dir(user.name, vm.uuid)

    if request.method == 'GET':
        if 'type' in request.GET:
            if request.GET['type'] == 'xml':
                xml_path = os.path.join(vm_dir,
                        '%s/domain.xml' % vm.current_version)
                with open(xml_path, 'r') as file:
                    data = file.read()
                return HttpResponse(data, content_type='text/xml')

    elif request.method == 'POST':
        # Check if correct lock is provided
        data = json.loads(request.body)
        if 'key' not in data:
            return HttpResponseBadRequest('No key provided')
        if vm.lock != None:
            lock = vm.lock
            vm.lock = None
            vm.save()
            lock.delete()
        else:
            return HttpResponseForbidden('No lock for provided key')

        # Checkin
        if not vm.uncommitted_changes:
            data = vm.as_json()
            return HttpResponse(json.dumps(data),
                    content_type='application/json')

        version = int(version)
        if version == vm.current_version:
            # Copy staging area to new version
            staging_dir = _get_staging_dir(vm.user.name, vm.uuid, vm.current_version)
            old_version_path = os.path.join(vm_dir, str(vm.current_version))
            new_version_path = os.path.join(vm_dir, str(vm.current_version + 1))
            shutil.move(staging_dir, new_version_path)
            _setup_staging_dir(staging_dir)
            shutil.copy2(os.path.join(old_version_path, 'domain.xml'),
                    new_version_path)

            # Update new image sizes
            with open(os.path.join(new_version_path, 'disk/size'), 'r') as file:
                disk_size = int(file.readline())
            with open(os.path.join(new_version_path, 'memory/size'), 'r') as file:
                memory_size = int(file.readline())

            vm.current_version += 1
            vm.disk_size = disk_size
            vm.memory_size = memory_size
            vm.uncommitted_changes = False
            vm.save()

            # Make staging area of new version
            staging_dir = _get_staging_dir(vm.user.name, vm.uuid, vm.current_version)
            _setup_staging_dir(staging_dir)

            # Create new version object
            data = json.loads(request.body)
            if 'comment' in data:
                comment = data['comment']
            else:
                comment = 'No comment provided.'
            version = Version(vm=vm, number=vm.current_version, disk_size=disk_size,
                    memory_size=memory_size, comment=comment)
            version.save()

            data = vm.as_json()
            return HttpResponse(json.dumps(data), content_type='application/json')
        else:
            # Process size files first
            disk_size_path = os.path.join(staging_dir, 'disk/size')
            with open(disk_size_path, 'r') as file:
                disk_size = int(file.readline())
            os.remove(disk_size_path)
            memory_size_path = os.path.join(staging_dir, 'memory/size')
            with open(memory_size_path, 'r') as file:
                memory_size = int(file.readline())
            os.remove(memory_size_path)

            # Create new VM
            ver = get_object_or_404(Version, vm=vm, number=version)
            disk_size = ver.disk_size
            memory_size = ver.memory_size
            new_vm = VM(basevm=vm.basevm, name='%s from v%s' % (vm.name, version),
                    user=user, disk_size=disk_size, memory_size=memory_size)
            new_vm.save()

            # Start versions off from 1 or the original?
            new_ver = Version(vm=new_vm, number=new_vm.current_version,
                    disk_size=disk_size, memory_size=memory_size,
                    comment='Branched from %s version %s' % (vm.name, version))
            new_ver.save()

            # Set up directories
            _setup_vm_dir_from_vm(user, vm, version, new_vm,
                    new_vm.current_version)

            new_vm_dir = _get_vm_dir(user.name, new_vm.uuid)

            staging_dir = _get_staging_dir(vm.user.name, vm.uuid, version)
            old_version_path = os.path.join(vm_dir, str(version))
            new_version_path = os.path.join(new_vm_dir,
                    str(new_vm.current_version))

            # Copy modified chunks over
            for image in 'disk', 'memory':
                image_dir = os.path.join(staging_dir, image)
                new_image_dir = os.path.join(new_version_path, image)

                for chunk_dir in os.listdir(image_dir):
                    dir = os.path.join(image_dir, chunk_dir)
                    new_dir = os.path.join(new_image_dir, chunk_dir)
                    for chunk in os.listdir(dir):
                        chunk_path = os.path.join(dir, chunk)
                        new_chunk_path = os.path.join(new_dir, chunk)
                        # The chunks are all symlinks, so they must be
                        # deleted or else the linked chunk will be modified
                        if os.path.isfile(new_chunk_path):
                            os.remove(new_chunk_path)
                        shutil.copy2(chunk_path, new_chunk_path)
                shutil.rmtree(image_dir)

            # (Re)create staging dirs
            _setup_staging_dir(staging_dir)
            new_staging_dir = _get_staging_dir(new_vm.user.name, new_vm.uuid,
                    new_vm.current_version)
            _setup_staging_dir(new_staging_dir)

            # Remove this at some point?
            shutil.copy2(os.path.join(old_version_path, 'domain.xml'),
                    new_version_path)

    return HttpResponse()

''' Used to get the size of an image or send size of staged image'''
@require_http_methods(['GET', 'POST'])
def size(request, uuid, version, image):
    user = _get_user(request)
    vm = get_object_or_404(VM, uuid=uuid)
    version = int(version)

    if image not in ('disk', 'memory'):
        return HttpResponseBadRequest('Invalid image type')

    if request.method == 'GET':
        ver = get_object_or_404(Version, vm=vm, number=version)
        if image == 'disk':
            data = str(ver.disk_size)
        elif image == 'memory':
            data = str(ver.memory_size)
        return HttpResponse(data, content_type='text/plain')
    elif request.method == 'POST':
        staging_dir = _get_staging_dir(user.name, uuid, version)
        size_file_path = os.path.join(staging_dir, image, 'size')

        # Some stuff will be changed here for background upload
        with open(size_file_path, 'w') as file:
            file.write(request.body)
        # Has memory size changed?
        if image == 'disk':
            vm.uncommitted_changes = (int(request.body) != vm.disk_size)
        elif image == 'memory':
            vm.uncommitted_changes = (int(request.body) != vm.memory_size)

        vm.save()
        return HttpResponse()

''' Fetches or stages the chunk of the current version of the vm '''
#TODO: add secret key to curl in vmnetfs/transport.c
@require_http_methods(['GET', 'PUT',])
def chunk(request, uuid, version, image, num):
    vm = get_object_or_404(VM, uuid=uuid)
    version = int(version)

    if request.method == 'GET':
        if image not in ('disk', 'memory'):
            raise Http404
        dir = _get_chunk_dir(num)
        vm_dir = _get_vm_dir(vm.user.name, vm.uuid)

        # Make sure version exists
        ver = get_object_or_404(Version, vm=vm, number=version)
        if image == 'disk':
            size = ver.disk_size
        else:
            size = ver.memory_size

        # Iterate through versions to find latest version of chunk
        curr = version
        while curr > 0:
            chunk_path = os.path.join(vm_dir,
                    '%s/%s/%s/%s' % (curr, image, dir, num))
            if os.path.isfile(chunk_path):
                break
            curr -= 1
        if curr == 0:
            return HttpResponseNotFound('Chunk not found')

        # Currently returns partial chunks if last chunk is partial. The
        # alternative is to modify vmnetfs to always request full chunks, but
        # this change was easier to make (For now)
        with open(chunk_path, 'r') as file:
            data = file.read()
        assert len(data) == settings.CHUNK_SIZE
        count = size - int(num) * settings.CHUNK_SIZE
        if count < settings.CHUNK_SIZE:
            data = data[:count]
        return HttpResponse(data, content_type='text/plain')
    elif request.method == 'PUT':
        chunk = request.body
        staging_dir = _get_staging_dir(vm.user.name, vm.uuid, version)
        chunk_dir = os.path.join(staging_dir, image, str(_get_chunk_dir(num)))
        if not os.path.exists(chunk_dir):
            os.makedirs(chunk_dir)
        chunk_path = os.path.join(chunk_dir, num)
        with open(chunk_path, 'w') as file:
            file.write(chunk)
        return HttpResponse()

''' Claims the lock on a VM. If the request asks for force unlock, just delete
    existing lock and return a new one. Requires a 'machine name' field to set
    as the owner. The client may change this value while a lock is held, and the
    server will not know. Checkin does not require a owner, only the key. The
    owner is only for the user to have more info about the state of the vm/
    '''
@require_http_methods(['POST',])
def checkout(request, uuid):
    user = _get_user(request)
    vm = get_object_or_404(VM, uuid=uuid)

    data = json.loads(request.body)
    if 'machine name' not in data:
        return HttpResponseBadRequest('Missing machine name')

    # Check if lock is already claimed by someone else
    if vm.lock != None:
        # Check if its the same machine
        if vm.lock.owner == data['machine name']:
            return HttpResponse(vm.lock.key)
        elif 'force' in data and data['force'] == True:
            lock = vm.lock
            new_lock = Lock(owner=data['machine name'])
            new_lock.save()
            vm.lock = new_lock
            vm.save()
            lock.delete()
        else:
            return HttpResponseForbidden('Lock already claimed by client')
    # Otherwise, create new lock and return it
    else:
        lock = Lock(owner=data['machine name'])
        lock.save()
        vm.lock = lock
        vm.save()

    return HttpResponse(lock.key)

@require_http_methods(['POST',])
def create_vm_from_base(request):
    user = _get_user(request)

    if 'uuid' not in request.POST or 'name' not in request.POST:
        HttpResponseBadRequest('Missing uuid or name')

    # Create db objects
    basevm = get_object_or_404(BaseVM, uuid=request.POST['uuid'])
    name = request.POST['name']
    if name == '':
        name = basevm.name
    vm = VM(basevm=basevm, name=name, user=user,
            disk_size=basevm.disk_size, memory_size=basevm.memory_size)
    vm.save()
    version = Version(vm=vm, number=1, disk_size=basevm.disk_size,
            memory_size=basevm.memory_size, comment="Created from basevm",
            date_created=vm.date_created)
    version.save()

    # Set up storage directory (assume user directory already created)
    vm_dir = os.path.join(settings.STORAGE_DIR, 'vm/%s/%s' % (user, vm.uuid))
    os.makedirs(vm_dir)

    info_file_path = os.path.join(vm_dir, 'info')
    info = {'Name': vm.name, 'Base vm name': vm.basevm.name}
    with open(info_file_path, 'w') as file:
        file.write(json.dumps(info))
        file.write('\n')

    staging_dir = _get_staging_dir(user.name, vm.uuid, vm.current_version)
    _setup_staging_dir(staging_dir)

    # Create symlink to basevm directory because these files should not be
    # changed when checking in
    version_dir = os.path.join(vm_dir, str(vm.current_version)) # version is 1
    src = os.path.join(settings.STORAGE_DIR, 'basevm/%s' % basevm.uuid)
    os.symlink(src, version_dir)

    return HttpResponse()

@require_http_methods(['GET',])
def version(request, uuid):
    user = _get_user(request)
    versions = Version.objects.filter(vm__uuid=uuid)

    data = OrderedDict()
    for version in versions:
        data[version.number] = version.as_json()

    return HttpResponse(json.dumps(data), content_type='application/json')


# TODO: Create vm/user directory when registering
