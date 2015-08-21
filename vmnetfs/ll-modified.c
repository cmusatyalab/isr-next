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
#include <stdio.h>
#include <glib/gstdio.h>

#include <time.h>

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

static bool is_uploaded(struct vmnetfs_image *img, uint64_t chunk)
{
    struct stat buf;
    const char *file;
    int statchmod;

    file = get_file(img, chunk);
    stat(file, &buf);
    return buf.st_mode & S_ISVTX;

    printf("chmod: %o\n", statchmod);
}

static void set_uploaded_file(char *file, bool uploaded)
{
    struct stat buf;
    int statchmod;

    stat(file, &buf);
    // BUGGED?
    statchmod = buf.st_mode & (S_IRWXU | S_IRWXG | S_IRWXO);
    if (uploaded) {
        chmod(file, statchmod | S_ISVTX);
    }
    else {
        chmod(file, statchmod & ~S_ISVTX);
    }
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
            // fputs(g_strdup_printf("%s %d %d | %d %d | %d %d\n", file, *file, *endptr, chunk, chunks, dir_num, get_dir_num(chunks)), f);
            g_set_error(err, VMNETFS_IO_ERROR, VMNETFS_IO_ERROR_INVALID_CACHE,
                    "Invalid modified cache entry %s/%s", path, file);
            g_dir_close(dir);
            return false;
        }
        _vmnetfs_bit_set(img->modified_map, chunk);

        /// Update stats
        _vmnetfs_u64_stat_increment(img->chunks_modified, 1);
        if (!is_uploaded(img, chunk)) {
            _vmnetfs_u64_stat_increment(img->chunks_modified_not_uploaded, 1);
        }
        else {
            _vmnetfs_bit_notify_plus_minus(img->uploaded_map, chunk, 1);
        }
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
    char *uploaded_base;

    if (!mkdir_with_parents(img->modified_base, err)) {
        return false;
    }

    dir = g_dir_open(img->modified_base, 0, err);
    if (dir == NULL) {
        return false;
    }

    img->modified_map = _vmnetfs_bit_new(img->bitmaps, true);
    img->uploaded_map = _vmnetfs_bit_new(img->bitmaps, false);
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

    // Checkin?
    return true;
}

void _vmnetfs_ll_modified_destroy(struct vmnetfs_image *img)
{
    _vmnetfs_bit_free(img->modified_map);
    _vmnetfs_bit_free(img->uploaded_map);
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
    close(fd);
    if (ret) {
        /* If file was not created before, it could not have been uploaded */
        if (!_vmnetfs_bit_test(img->modified_map, chunk)) {
            _vmnetfs_u64_stat_increment(img->chunks_modified, 1);
            _vmnetfs_u64_stat_increment(img->chunks_modified_not_uploaded, 1);
        }
        else {
            /* Chunk was already uploaded, so unset the uploaded bit */
            if (is_uploaded(img, chunk)) {
                void *data;
                int len;
                data = g_malloc(img->chunk_size);
                g_file_get_contents(file, &data, &len, err);
                g_assert(len == img->chunk_size);
                g_file_set_contents(file, data, len, err);
                g_free(data);
                _vmnetfs_u64_stat_increment(img->chunks_modified_not_uploaded, 1);
                _vmnetfs_bit_notify_plus_minus(img->uploaded_map, chunk, 0);
            }
        }
        _vmnetfs_bit_set(img->modified_map, chunk);
    }
    // ret = g_file_set_contents(file, data, length, err);

out:
    g_free(file);
    g_free(dir);
    return ret;
}

bool _vmnetfs_ll_modified_set_size(struct vmnetfs_image *img,
        uint64_t current_size, uint64_t new_size, GError **err)
{
    /* If we're truncating the new last chunk, it must be in the modified
       cache to ensure that subsequent expansions don't reveal the truncated
       part. */
    g_assert(new_size > current_size ||
            new_size % img->chunk_size == 0 ||
            _vmnetfs_bit_test(img->modified_map, new_size / img->chunk_size));

    uint64_t chunk;
    uint64_t current_chunks;
    uint64_t new_chunks;
    char *dir;
    char *file;
    bool ret;
    int fd;

    current_chunks = (current_size + img->chunk_size - 1) / img->chunk_size;
    new_chunks = (new_size + img->chunk_size - 1) / img->chunk_size;

    if (new_size > current_size) {
        for (chunk = current_chunks; chunk < new_chunks; chunk++) {
            dir = get_dir(img, chunk);
            file = get_file(img, chunk);

            ret = mkdir_with_parents(dir, err);
            if (!ret) {
                goto out;
            }
            fd = open(file, O_CREAT | O_WRONLY, S_IRUSR | S_IWUSR);
            if (fd == -1) {
                g_set_error(err, G_FILE_ERROR, g_file_error_from_errno(errno),
                        "Couldn't open to write new modified %s: %s", file, strerror(errno));
                goto out;
            }
            if (ftruncate(fd, img->chunk_size)) {
                goto out;
            }
            close(fd);
            g_free(dir);
            g_free(file);

            _vmnetfs_u64_stat_increment(img->chunks_modified, 1);
            _vmnetfs_u64_stat_increment(img->chunks_modified_not_uploaded, 1);
        }
    }
    else {
        /* Special case for the last chunks */
        chunk = new_chunks;
        dir = get_dir(img, chunk);
        file = get_file(img, chunk);
        if (g_file_test(file, G_FILE_TEST_EXISTS)) {
            int partial;
            /* 0 <= partial < img->chunk_size */
            partial = chunk * img->chunk_size - new_size;
            if (partial > 0) {
                int new_size = img->chunk_size - partial;
                fd = open(file, O_WRONLY, S_IRUSR | S_IWUSR);
                if (fd == -1) {
                    g_set_error(err, G_FILE_ERROR, g_file_error_from_errno(errno),
                            "Couldn't open to write partial modified %s: %s", file, strerror(errno));
                    goto out;
                }
                if (ftruncate(fd, new_size)) {
                    goto out;
                }
                if (ftruncate(fd, img->chunk_size)) {
                    goto out;
                }
                close(fd);
            }
        }
        g_free(dir);
        g_free(file);
        for (chunk = new_chunks+1; chunk < current_chunks; chunk++) {
            dir = get_dir(img, chunk);
            file = get_file(img, chunk);

            if (g_file_test(file, G_FILE_TEST_EXISTS)) {
                if (remove(file)) {
                    goto out;
                }

                // decrement stats (??)
                _vmnetfs_u64_stat_decrement(img->chunks_modified, 1);
                if (!is_uploaded(img, chunk)) {
                    _vmnetfs_u64_stat_increment(img->chunks_modified_not_uploaded, 1);
                }

            }
            g_free(dir);
            g_free(file);
        }

    }
    return true;

out:
     g_free(dir);
     g_free(file);
     return false;
}

bool _vmnetfs_ll_modified_upload(struct vmnetfs_image *img, GError **err) {
    GDir *root_dir, *chunk_dir;
    char *name, *chunk_dir_path, *file;
    char *endptr;
    uint64_t chunk;
    FILE *chunk_file;
    char *chunk_path;
    bool ret;

    struct timespec start, end;
    struct timespec temp;

    root_dir = g_dir_open(img->modified_base, 0, err);
    if (root_dir == NULL) {
        goto out;
    }

    /* Read the chunk directories 0, 4096, ... */
    while (true) {
        while ((name = g_dir_read_name(root_dir)) != NULL) {
            chunk_dir_path = g_strdup_printf("%s/%s", img->modified_base, name);

            /* Iterate through chunk directories */
            if (g_file_test(chunk_dir_path, G_FILE_TEST_IS_DIR)) {
                chunk_dir = g_dir_open(chunk_dir_path, 0, err);
                while ((file = g_dir_read_name(chunk_dir)) != NULL) {
                    clock_gettime(CLOCK_MONOTONIC, &temp);

                    /* Check if chunk is already uploaded */
                    chunk = g_ascii_strtoull(file, &endptr, 10);

                    if (is_uploaded(img, chunk)) {
                        continue;
                    }
                    chunk_path = g_strdup_printf("%s/%s", chunk_dir_path, file);

                    /* Read chunk and upload */
                    chunk_file = fopen(chunk_path, "r");

                    /* Mark the file as uploaded before so that if any
                     * modifications are made before upload finished, they are
                     * still marked dirty */
                    set_uploaded_file(chunk_path, true);

                    clock_gettime(CLOCK_MONOTONIC, &start);
                    clock_t start2 = clock();

                    _vmnetfs_io_put_data(img, img->cpool, chunk, chunk_file, err);
                    if (!img->checkin) {
                      // usleep(1000*100);
                    }

                    clock_t stop = clock();
                    double elapsed = (double)(stop - start2) * 1000.0 / CLOCKS_PER_SEC;

                    clock_gettime(CLOCK_MONOTONIC, &end);

                    if (*err != NULL) {
                    }

                    fclose(chunk_file);

                    /* Update stats and streams */
                    _vmnetfs_u64_stat_decrement(img->chunks_modified_not_uploaded, 1);
                    _vmnetfs_bit_notify_plus_minus(img->uploaded_map, chunk, 1);

                    /* Cleanup */
                    g_free(chunk_path);

                    // TEMP THROTTLE FOR NON CHECKIN
                    if (!img->checkin) {
                        // usleep in microseconds (millionth)
                        unsigned int wait_us;
                        uint64_t diff = 1000000000L * (end.tv_sec - start.tv_sec) + end.tv_nsec - start.tv_nsec;
                        diff = diff / 1000;
                        wait_us = (unsigned int) ((double) diff / img->rate) - diff;
                        // fputs(g_strdup_printf("transfer took %u, %u %f %f waiting %u microseconds\n", diff,
                        //           diff/1000, img->rate, (double)diff / img->rate, (unsigned int)wait_us), f);
                        // usleep((unsigned int)wait_us);
                    }
                }
                g_dir_close(chunk_dir);
            }
            g_free(chunk_dir_path);
        }
        g_dir_rewind(root_dir);
        sleep(1);
    }

out:
    g_dir_close(root_dir);

    return true;
}
