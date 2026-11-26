// SPDX-License-Identifier: Apache-2.0
#define _POSIX_C_SOURCE 200809L

#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static int copy_receipt(const char *dest) {
    FILE *in = fopen("/fixture-mcu-receipt.json", "rb");
    if (!in)
        return access("/fixture-mcu-no-receipt", F_OK) != 0;
    char temporary[128];
    int length = snprintf(temporary, sizeof temporary, "%s.tmp", dest);
    if (length < 0 || (size_t)length >= sizeof temporary) {
        fclose(in);
        return 1;
    }
    FILE *out = fopen(temporary, "wbx");
    if (!out) {
        fclose(in);
        return 1;
    }
    char buffer[2048];
    size_t bytes = fread(buffer, 1, sizeof buffer, in);
    int failed = ferror(in) || !feof(in);
    if (fclose(in))
        failed = 1;
    if (!failed && fwrite(buffer, 1, bytes, out) != bytes)
        failed = 1;
    if (fclose(out))
        failed = 1;
    if (failed) {
        unlink(temporary);
        return 1;
    }
    return rename(temporary, dest) != 0;
}

int main(int argc, char **argv) {
    if (argc != 11 || strcmp(argv[1], "--package") ||
        strncmp(argv[2], "/var/lib/y4-board/firmware-", 26) ||
        strcmp(argv[5], "--host-version") || strcmp(argv[6], "0.0.0-fixture") ||
        strcmp(argv[7], "--status") || strncmp(argv[8], "/run/y4-board/update-", 21) ||
        strcmp(argv[9], "--sequence-floor"))
        return 100;

    FILE *package = fopen(argv[2], "rb");
    char first[16];
    if (!package)
        return 101;
    size_t bytes = fread(first, 1, sizeof first, package);
    if (fclose(package) || bytes != sizeof first || memcmp(first, "synthetic-mcu-v1", 16))
        return 101;

    FILE *control = fopen("/fixture-mcu-action", "rb");
    if (!control)
        return 102;
    int action = fgetc(control);
    if (fclose(control))
        return 102;

    sigset_t stops;
    sigemptyset(&stops);
    sigaddset(&stops, SIGTERM);
    if (sigprocmask(SIG_BLOCK, &stops, NULL) != 0)
        return 103;
    if (action == 'H' && copy_receipt(argv[8]))
        return 104;
    FILE *started = fopen("/fixture-mcu-pid", "wx");
    if (!started)
        return 105;
    int result = fprintf(started, "%ld\n", (long)getpid());
    if (fclose(started) || result < 0)
        return 105;

    if (action == 'T' || action == 'H') {
        int received = 0;
        if (sigwait(&stops, &received) != 0 || received != SIGTERM)
            return 106;
    }
    if (action != 'H' && copy_receipt(argv[8]))
        return 107;
    return action == 'S' ? 0 : 1;
}
