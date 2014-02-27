/*
 * vmnetfs - virtual machine network execution virtual filesystem
 *
 * Copyright (C) 2006-2012 Carnegie Mellon University
 *
 * This program is free software; you can redistribute it and/or modify it
 * under the terms of version 2 of the GNU General Public License as published
 * by the Free Software Foundation.  A copy of the GNU General Public License
 * should have been distributed along with this program in the file
 * COPYING.
 *
 * This program is distributed in the hope that it will be useful, but
 * WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
 * or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
 * for more details.
 */

#include <sys/types.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include "vmnetfs-private.h"
#include <inttypes.h>
#include <fcntl.h>
#include <sys/stat.h>


#define CHUNKS_PER_DIR 4096

static bool mkdir_with_parents(const char *dir, GError **err)
{
    if (g_mkdir_with_parents(dir, 0700)) {
        g_set_error(err, G_FILE_ERROR, g_file_error_from_errno(errno),
                "Couldn't create %s: %s", dir, strerror(errno));
        return false;
    }
    return true;
}

static uint64_t get_dir_num(uint64_t chunk)
{
    return chunk / CHUNKS_PER_DIR * CHUNKS_PER_DIR;
}

static char *get_dir(struct vmnetfs_image *img, uint64_t chunk)
{
    return g_strdup_printf("%s/%"PRIu64, img->modified_base, get_dir_num(chunk));
}

static char *get_file(struct vmnetfs_image *img, uint64_t chunk)
{
    return g_strdup_printf("%s/%"PRIu64"/%"PRIu64, img->modified_base,
            get_dir_num(chunk), chunk);
}

static bool set_present_from_directory(struct vmnetfs_image *img,
        const char *path, uint64_t dir_num, GError **err)
{
    GDir *dir;
    const char *file;
    uint64_t chunk;
    uint64_t chunks;
    char *endptr;

    chunks = (img->initial_size + img->chunk_size - 1) / img->chunk_size;
    dir = g_dir_open(path, 0, err);
    if (dir == NULL) {
        return false;
    }

    while ((file = g_dir_read_name(dir)) != NULL) {
        chunk = g_ascii_strtoull(file, &endptr, 10);
        if (chunk > chunks) {
            g_set_error(err, VMNETFS_IO_ERROR, VMNETFS_IO_ERROR_INVALID_CACHE,
                    "Found modified cache entry that should have been deleted %s/%"PRIu64, path, chunk);
            g_dir_close(dir);
            return false;
        }
        if (*file == 0 || *endptr != 0 ||
                dir_num != get_dir_num(chunk)) {
            g_set_error(err, VMNETFS_IO_ERROR, VMNETFS_IO_ERROR_INVALID_CACHE,
                    "Invalid modified cache entry %s/%s", path, file);
            g_dir_close(dir);
            return false;
        }
        _vmnetfs_bit_set(img->modified_map, chunk);
    }
    g_dir_close(dir);
    return true;
}

bool _vmnetfs_ll_modified_init(struct vmnetfs_image *img, GError **err)
{
    GDir *dir;
    const char *name;
    char *endptr;
    uint64_t dir_num;
    char *path;

    if (!mkdir_with_parents(img->modified_base, err)) {
        return false;
    }

    dir = g_dir_open(img->modified_base, 0, err);
    if (dir == NULL) {
        return false;
    }
    img->modified_map = _vmnetfs_bit_new(img->bitmaps, true);
    while ((name = g_dir_read_name(dir)) != NULL) {
        path = g_strdup_printf("%s/%s", img->modified_base, name);
        dir_num = g_ascii_strtoull(name, &endptr, 10);
        if (*name != 0 && *endptr == 0 && g_file_test(path,
                G_FILE_TEST_IS_DIR)) {
            if (!set_present_from_directory(img, path, dir_num, err)) {
                g_free(path);
                g_dir_close(dir);
                _vmnetfs_bit_free(img->modified_map);
                return false;
            }
        }
        g_free(path);
    }
    g_dir_close(dir);
    return true;
}

void _vmnetfs_ll_modified_destroy(struct vmnetfs_image *img)
{
    _vmnetfs_bit_free(img->modified_map);
    close(img->write_fd);
}

bool _vmnetfs_ll_modified_read_chunk(struct vmnetfs_image *img,
        uint64_t image_size, void *data, uint64_t chunk, uint32_t offset,
        uint32_t length, GError **err)
{
    char *file;
    int fd;
    bool ret;

    g_assert(_vmnetfs_bit_test(img->modified_map, chunk));
    g_assert(offset < img->chunk_size);
    g_assert(offset + length <= img->chunk_size);
    g_assert(chunk * img->chunk_size + offset + length <= image_size);

    file = get_file(img, chunk);
    fd = open(file, O_RDONLY);
    if (fd == -1) {
        g_set_error(err, G_FILE_ERROR, g_file_error_from_errno(errno),
                "Couldn't open to read modified %s: %s", file, strerror(errno));
        g_free(file);
        return false;
    }
    ret = _vmnetfs_safe_pread(file, fd, data, length, offset, err);

    close(fd);
    g_free(file);
    return ret;

}

bool _vmnetfs_ll_modified_write_chunk(struct vmnetfs_image *img,
        uint64_t image_size, const void *data, uint64_t chunk,
        uint32_t offset, uint32_t length, GError **err)
{
    g_assert(_vmnetfs_bit_test(img->modified_map, chunk) ||
            (offset == 0 && length == MIN(img->chunk_size,
            img->initial_size - chunk * img->chunk_size)));
    g_assert(offset < img->chunk_size);
    g_assert(offset + length <= img->chunk_size);
    g_assert(chunk * img->chunk_size + offset + length <= image_size);

    char *dir;
    char *file;
    bool ret;
    int fd;

    dir = get_dir(img, chunk);
    file = get_file(img, chunk);

    ret = mkdir_with_parents(dir, err);
    if (!ret) {
        goto out;
    }

    fd = open(file, O_CREAT | O_WRONLY, S_IRUSR | S_IWUSR);
    if (fd == -1) {
        g_set_error(err, G_FILE_ERROR, g_file_error_from_errno(errno),
                "Couldn't open to write modified %s: %s", file, strerror(errno));
        g_free(file);
        return false;
    }

    ret = _vmnetfs_safe_pwrite("chunk", fd, data, length, offset, err);
    if (ret)
        _vmnetfs_bit_set(img->modified_map, chunk);
    close(fd);
    // ret = g_file_set_contents(file, data, length, err);

out:
    g_free(file);
    g_free(dir);
    return ret;
}

bool _vmnetfs_ll_modified_set_size(struct vmnetfs_image *img,
        uint64_t current_size, uint64_t new_size, GError **err)
{
    return true;
    /* If we're truncating the new last chunk, it must be in the modified
       cache to ensure that subsequent expansions don't reveal the truncated
       part. */
    g_assert(new_size > current_size ||
            new_size % img->chunk_size == 0 ||
            _vmnetfs_bit_test(img->modified_map, new_size / img->chunk_size));

    if (ftruncate(img->write_fd, new_size)) {
        g_set_error(err, G_FILE_ERROR, g_file_error_from_errno(errno),
                "Couldn't truncate image: %s", strerror(errno));
        return false;
    }

    return true;
}
