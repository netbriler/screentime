/*
 * screentime-collector: the only part of the Screen Time tracker that gets Full Disk Access.
 *
 * It hands the tracker exactly the Screen Time data it needs, so the Python tracker can work with no access to
 * anything else. It does this and nothing more:
 *
 *   iPhone/iPad (Biome): copies the small, append-only segment files of
 *     ~/Library/Biome/streams/restricted/App.InFocus/remote/
 *     -> ~/Library/Application Support/screentime/snapshot/Library/Biome/...
 *     Only new or changed files are copied; the tracker's Python code parses them. The synced devices' ids, names and
 *     platforms come from one fixed, read-only query on ~/Library/Biome/sync/sync.db, written to
 *     snapshot/biome/devices.json; that database (tens of MB) is never copied.
 *
 *   This Mac (knowledgeC): runs one fixed, read-only query on
 *     ~/Library/Application Support/Knowledge/knowledgeC.db
 *     and writes the new app-usage rows to snapshot/knowledgec/new-<time>.jsonl, plus the synced devices' hardware
 *     models to snapshot/knowledgec/peers.json. The database itself is never copied.
 *
 * The paths and the queries are fixed here. The home folder comes from the account database, not from $HOME, and
 * the program takes no arguments, so nothing can point it at other files. It never opens a network connection.
 * Everything it writes is readable by its owner only.
 *
 * macOS ties Full Disk Access to this binary's code signature: if the file is replaced, the grant stops applying.
 */

#include <copyfile.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pwd.h>
#include <sqlite3.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <unistd.h>

#ifndef SCREENTIME_TEST_HOME
#define SCREENTIME_TEST_HOME NULL  /* tests build with a fake home folder; the real build always uses the account */
#endif

static char home[PATH_MAX];
static int copied = 0, failed = 0, rows = 0;
static long long bytes_written = 0;

static int join(char *out, const char *a, const char *b) {
    int n = snprintf(out, PATH_MAX, "%s/%s", a, b);
    return n > 0 && n < PATH_MAX ? 0 : -1;
}

static int make_dirs(const char *path) {
    char buf[PATH_MAX];
    if (strlen(path) >= sizeof buf) return -1;
    strcpy(buf, path);
    for (char *p = buf + 1; *p; p++) {
        if (*p == '/') {
            *p = '\0';
            if (mkdir(buf, 0700) != 0 && errno != EEXIST) return -1;
            *p = '/';
        }
    }
    return mkdir(buf, 0700) != 0 && errno != EEXIST ? -1 : 0;
}

static int same(const struct stat *a, const struct stat *b) {
    return a->st_size == b->st_size && a->st_mtimespec.tv_sec == b->st_mtimespec.tv_sec &&
           a->st_mtimespec.tv_nsec == b->st_mtimespec.tv_nsec;
}

/* Copy one regular file if it changed; returns 1 when copied, 0 when unchanged or absent, -1 on error. */
static int copy_if_changed(const char *src, const char *dst) {
    struct stat s, d;
    if (lstat(src, &s) != 0) return errno == ENOENT ? 0 : -1;
    if (!S_ISREG(s.st_mode)) return 0;
    if (lstat(dst, &d) == 0 && same(&s, &d)) return 0;
    char tmp[PATH_MAX];
    if (snprintf(tmp, sizeof tmp, "%s.part", dst) >= (int)sizeof tmp) return -1;
    unlink(tmp);
    if (copyfile(src, tmp, NULL, COPYFILE_DATA) != 0) {
        unlink(tmp);
        return -1;
    }
    struct timespec times[2] = {s.st_atimespec, s.st_mtimespec};
    if (chmod(tmp, 0600) != 0 || utimensat(AT_FDCWD, tmp, times, 0) != 0 || rename(tmp, dst) != 0) {
        unlink(tmp);
        return -1;
    }
    bytes_written += s.st_size;
    return 1;
}

static void count(int result) {
    if (result > 0) copied++;
    if (result < 0) failed++;
}

/* Mirror a directory of regular files and subdirectories, up to `depth` levels; files gone from the source go too. */
static void mirror(const char *src, const char *dst, int depth) {
    DIR *dir = opendir(src);
    if (!dir) {
        if (errno != ENOENT) failed++;
        return;
    }
    if (make_dirs(dst) != 0) {
        failed++;
        closedir(dir);
        return;
    }
    struct dirent *entry;
    while ((entry = readdir(dir))) {
        if (entry->d_name[0] == '.') continue;
        char s[PATH_MAX], d[PATH_MAX];
        if (join(s, src, entry->d_name) || join(d, dst, entry->d_name)) continue;
        struct stat st;
        if (lstat(s, &st) != 0) continue;
        if (S_ISDIR(st.st_mode) && depth > 0) mirror(s, d, depth - 1);
        else if (S_ISREG(st.st_mode)) count(copy_if_changed(s, d));
    }
    closedir(dir);
    /* Remove copies whose source is gone (Biome deletes old segments). */
    DIR *out = opendir(dst);
    if (!out) return;
    while ((entry = readdir(out))) {
        if (entry->d_name[0] == '.') continue;
        char s[PATH_MAX], d[PATH_MAX];
        if (join(s, src, entry->d_name) || join(d, dst, entry->d_name)) continue;
        struct stat st, dt;
        if (lstat(s, &st) != 0 && errno == ENOENT && lstat(d, &dt) == 0 && S_ISREG(dt.st_mode)) unlink(d);
    }
    closedir(out);
}

/* ---- knowledgeC: one fixed query, new rows only -------------------------------------------------------------- */

/* The same rows the tracker's knowledgeC source reads: this Mac's own /app/usage intervals (rows synced from other
 * devices carry a source device id), newer than the cursor. Rows are written when an interval ends, sometimes
 * minutes later, so the cursor is the creation date and the last hour is read again; the tracker drops repeats. */
static const char *USAGE_QUERY =
    "SELECT o.ZVALUESTRING, o.ZSTARTDATE, o.ZENDDATE, o.ZCREATIONDATE FROM ZOBJECT o "
    "LEFT JOIN ZSOURCE s ON s.Z_PK = o.ZSOURCE "
    "WHERE o.ZSTREAMNAME = '/app/usage' AND s.ZDEVICEID IS NULL AND o.ZCREATIONDATE > ? "
    "ORDER BY o.ZCREATIONDATE";
/* Synced devices from Biome's sync.db: id -> [IDS id, name, platform]. */
static const char *DEVICES_QUERY =
    "SELECT device_identifier, ids_device_identifier, name, platform FROM DevicePeer";
/* Hardware models of the synced iPhones and iPads, which the tracker uses to tell them apart. */
static const char *PEERS_QUERY =
    "SELECT ZRAPPORTID, ZMODEL FROM ZSYNCPEER WHERE ZRAPPORTID IS NOT NULL AND ZMODEL IS NOT NULL";
static const double OVERLAP_SECONDS = 3600.0;

static void json_string(FILE *f, const unsigned char *text) {
    fputc('"', f);
    for (const unsigned char *c = text ? text : (const unsigned char *)""; *c; c++) {
        if (*c == '"' || *c == '\\') fprintf(f, "\\%c", *c);
        else if (*c < 0x20) fprintf(f, "\\u%04x", *c);
        else fputc(*c, f);
    }
    fputc('"', f);
}

static double read_cursor(const char *path) {
    FILE *f = fopen(path, "r");
    double value = 0;
    if (f) {
        if (fscanf(f, "%lf", &value) != 1) value = 0;
        fclose(f);
    }
    return value;
}

/* Write `path` from a temporary file, so a reader never sees half of it. */
static FILE *open_part(const char *path, char *part) {
    if (snprintf(part, PATH_MAX, "%s.part", path) >= PATH_MAX) return NULL;
    return fopen(part, "w");
}

static int finish_part(FILE *f, const char *part, const char *path) {
    long size = ftell(f);
    if (fclose(f) != 0 || rename(part, path) != 0) {
        unlink(part);
        return -1;
    }
    if (size > 0) bytes_written += size;
    return 0;
}

/* Write the rows of `query` as a JSON object to dir/name: first column -> second column, or -> an array of the
 * remaining columns when there are more. Rewritten only when it changes. */
static void export_map(sqlite3 *db, const char *query, const char *dir, const char *name) {
    sqlite3_stmt *stmt;
    if (sqlite3_prepare_v2(db, query, -1, &stmt, NULL) != SQLITE_OK) return;  /* older macOS: no such table */
    int columns = sqlite3_column_count(stmt);
    /* Built in memory first, so peers.json is rewritten only when it changes. */
    char *buffer = NULL;
    size_t length = 0;
    FILE *mem = open_memstream(&buffer, &length);
    if (!mem) {
        sqlite3_finalize(stmt);
        return;
    }
    fputc('{', mem);
    for (int first = 1; sqlite3_step(stmt) == SQLITE_ROW; first = 0) {
        if (!first) fputc(',', mem);
        json_string(mem, sqlite3_column_text(stmt, 0));
        fputc(':', mem);
        if (columns == 2) {
            json_string(mem, sqlite3_column_text(stmt, 1));
            continue;
        }
        fputc('[', mem);
        for (int i = 1; i < columns; i++) {
            if (i > 1) fputc(',', mem);
            if (sqlite3_column_type(stmt, i) == SQLITE_INTEGER) fprintf(mem, "%lld", sqlite3_column_int64(stmt, i));
            else if (sqlite3_column_type(stmt, i) == SQLITE_NULL) fputs("null", mem);
            else json_string(mem, sqlite3_column_text(stmt, i));
        }
        fputc(']', mem);
    }
    fputs("}\n", mem);
    fclose(mem);
    sqlite3_finalize(stmt);

    char path[PATH_MAX], part[PATH_MAX];
    join(path, dir, name);
    FILE *old = fopen(path, "r");
    if (old) {
        char *current = malloc(length + 1);
        size_t got = current ? fread(current, 1, length + 1, old) : 0;
        int unchanged = current && got == length && memcmp(current, buffer, length) == 0;
        free(current);
        fclose(old);
        if (unchanged) {
            free(buffer);
            return;
        }
    }
    FILE *f = open_part(path, part);
    if (!f || fwrite(buffer, 1, length, f) != length || finish_part(f, part, path) != 0) failed++;
    free(buffer);
}

/* Open a database Apple owns read-only (it is never written; SQLite takes only its shared read lock), or NULL. */
static sqlite3 *open_readonly(const char *db_path, const char *dir, const char *label) {
    struct stat st;
    if (stat(db_path, &st) != 0) {
        if (errno != ENOENT) failed++;
        return NULL;
    }
    if (make_dirs(dir) != 0) {
        failed++;
        return NULL;
    }
    char uri[PATH_MAX + 32];
    snprintf(uri, sizeof uri, "file:%s?mode=ro", db_path);
    sqlite3 *db = NULL;
    if (sqlite3_open_v2(uri, &db, SQLITE_OPEN_READONLY | SQLITE_OPEN_URI, NULL) != SQLITE_OK) {
        fprintf(stderr, "%s: %s\n", label, db ? sqlite3_errmsg(db) : "cannot open");  /* no Full Disk Access yet */
        sqlite3_close(db);
        failed++;
        return NULL;
    }
    sqlite3_busy_timeout(db, 5000);
    return db;
}

static void export_biome_devices(const char *db_path, const char *dir) {
    sqlite3 *db = open_readonly(db_path, dir, "Biome sync.db");
    if (!db) return;
    export_map(db, DEVICES_QUERY, dir, "devices.json");
    sqlite3_close(db);
}

static void export_knowledge(const char *db_path, const char *dir) {
    sqlite3 *db = open_readonly(db_path, dir, "knowledgeC");
    if (!db) return;
    export_map(db, PEERS_QUERY, dir, "peers.json");

    char cursor_path[PATH_MAX];
    join(cursor_path, dir, "cursor");
    double cursor = read_cursor(cursor_path), newest = cursor;
    sqlite3_stmt *stmt;
    if (sqlite3_prepare_v2(db, USAGE_QUERY, -1, &stmt, NULL) != SQLITE_OK) {
        fprintf(stderr, "knowledgeC: %s\n", sqlite3_errmsg(db));
        sqlite3_close(db);
        failed++;
        return;
    }
    sqlite3_bind_double(stmt, 1, cursor - OVERLAP_SECONDS);

    struct timeval now;
    gettimeofday(&now, NULL);
    char name[64], path[PATH_MAX], part[PATH_MAX];
    snprintf(name, sizeof name, "new-%013lld.jsonl", (long long)now.tv_sec * 1000 + now.tv_usec / 1000);
    join(path, dir, name);
    FILE *f = NULL;
    int step;
    while ((step = sqlite3_step(stmt)) == SQLITE_ROW) {
        if (!f && !(f = open_part(path, part))) break;
        double created = sqlite3_column_double(stmt, 3);
        if (created > newest) newest = created;
        fputs("{\"bundle_id\":", f);
        json_string(f, sqlite3_column_text(stmt, 0));
        fprintf(f, ",\"start\":%.6f,\"end\":%.6f,\"created\":%.6f}\n", sqlite3_column_double(stmt, 1),
                sqlite3_column_double(stmt, 2), created);
        rows++;
    }
    int query_ok = step == SQLITE_DONE;
    sqlite3_finalize(stmt);
    sqlite3_close(db);
    if (!query_ok) {
        if (f) {
            fclose(f);
            unlink(part);
        }
        failed++;
        return;
    }
    if (f && finish_part(f, part, path) != 0) {
        failed++;
        return;
    }
    /* The cursor moves only after the rows are safely on disk. */
    if (newest > cursor) {
        FILE *c = open_part(cursor_path, part);
        if (!c || fprintf(c, "%.6f\n", newest) < 0 || finish_part(c, part, cursor_path) != 0) failed++;
    }
}

int main(int argc, char **argv) {
    (void)argv;
    if (argc != 1) {
        fprintf(stderr, "screentime-collector takes no arguments\n");
        return 2;
    }
    const char *test_home = SCREENTIME_TEST_HOME;
    struct passwd *account = getpwuid(getuid());
    const char *base = test_home ? test_home : account ? account->pw_dir : NULL;
    if (!base || strlen(base) >= sizeof home) return 2;
    strcpy(home, base);
    umask(077);

    char lib[PATH_MAX], out[PATH_MAX], s[PATH_MAX], d[PATH_MAX];
    snprintf(lib, sizeof lib, "%s/Library", home);
    snprintf(out, sizeof out, "%s/Library/Application Support/screentime/snapshot/Library", home);

    join(s, lib, "Biome/streams/restricted/App.InFocus/remote");
    join(d, out, "Biome/streams/restricted/App.InFocus/remote");
    mirror(s, d, 1);

    char devices_dir[PATH_MAX];
    join(s, lib, "Biome/sync/sync.db");
    snprintf(devices_dir, sizeof devices_dir, "%s/Library/Application Support/screentime/snapshot/biome", home);
    export_biome_devices(s, devices_dir);

    char knowledge[PATH_MAX], export_dir[PATH_MAX];
    snprintf(knowledge, sizeof knowledge, "%s/Application Support/Knowledge/knowledgeC.db", lib);
    snprintf(export_dir, sizeof export_dir, "%s/Library/Application Support/screentime/snapshot/knowledgec", home);
    export_knowledge(knowledge, export_dir);

    printf("{\"copied\": %d, \"rows\": %d, \"bytes_written\": %lld, \"failed\": %d}\n", copied, rows,
           bytes_written, failed);
    return failed ? 1 : 0;
}
