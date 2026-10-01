/* Bambu Studio 02.08 on Linux names its single-instance D-Bus object
 * "/com.bambulab/BambuStudio/InstanceCheck/Object<hash>". Dots aren't allowed in an
 * object path: libdbus registers it anyway, but refuses (aborts) to address a message
 * to it, so a second launch can never hand its file to the open window.
 *
 * Preloaded into the container (/etc/ld.so.preload), this rewrites that one prefix to
 * "/com/bambulab/" (same length) on both sides. Any other path passes through. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <string.h>

static const char BAD[] = "/com.bambulab/";
static const char GOOD[] = "/com/bambulab/";

static const char *fix(const char *path, char *buf, size_t size) {
    if (path == NULL || strncmp(path, BAD, sizeof BAD - 1) != 0 || strlen(path) >= size)
        return path;
    strcpy(buf, path);
    memcpy(buf, GOOD, sizeof GOOD - 1);
    return buf;
}

typedef unsigned int (*register_fn)(void *, const char *, const void *, void *, void *);
typedef void *(*method_call_fn)(const char *, const char *, const char *, const char *);

unsigned int dbus_connection_try_register_object_path(void *conn, const char *path,
                                                      const void *vtable, void *data, void *err) {
    static register_fn real;
    char buf[256];
    if (!real) real = (register_fn)dlsym(RTLD_NEXT, "dbus_connection_try_register_object_path");
    return real(conn, fix(path, buf, sizeof buf), vtable, data, err);
}

void *dbus_message_new_method_call(const char *dest, const char *path, const char *iface,
                                   const char *method) {
    static method_call_fn real;
    char buf[256];
    if (!real) real = (method_call_fn)dlsym(RTLD_NEXT, "dbus_message_new_method_call");
    return real(dest, fix(path, buf, sizeof buf), iface, method);
}
