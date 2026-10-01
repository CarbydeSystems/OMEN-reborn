/*
 * omen-rank — native (C) guess-rank estimator.
 *
 * Scores one or more passwords against a trained model (the same ip/cp/ep/ln
 * walk as omen/model.py's NgramModel.total_level) and, for each password that
 * the model can represent, estimates its guess rank: the number of candidates
 * the enumerator would emit before it (i.e. at a strictly lower total level),
 * capped at --rank-cap. This mirrors omen/score.py's PasswordScorer exactly —
 * same output shape as `omen eval --rank`, just native speed.
 *
 * The counting enumeration reuses the pruned recursive walk proven in
 * omen_enum.c (manifest/table loading, IP buckets, per-context CP ordering,
 * level-budget recursion) — duplicated here deliberately rather than shared
 * via a common header, so this file stays self-contained and omen_enum.c is
 * untouched. See native/omen_enum.c's own header comment for the algorithm.
 *
 * Build:  make -C native omen-rank   (see native/Makefile)
 * Usage:  omen-rank <model_dir> [-i FILE] [--rank-cap N] [password ...]
 *         -i FILE reads one password per line ('-' = stdin); passwords may
 *         also be given positionally. At least one of the two is required.
 */

/* Must precede every system header: exposes POSIX.1-2008 getline() under
 * strict -std=c11 (which otherwise hides it and only implicit-declares the
 * glibc symbol — compiles, but warns, and isn't portable to other libcs). */
#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t u64;

/* Must match omen/model.py: MANIFEST_MAGIC, FORMAT_VERSION, MAX_TABLE_ENTRIES. */
#define MANIFEST_MAGIC "OMN1"
#define FORMAT_VERSION 1
#define MAX_TABLE_ENTRIES (1u << 28)
#define MAX_PW_CHARS 512 /* generous; model.max_length is always far smaller */
#define DEFAULT_RANK_CAP 10000000ULL /* matches omen/cli.py's --rank-cap default */

/* One continuation option: emit `code` after the current context, at `level`. */
typedef struct {
    u8 code;
    u8 level;
} CodeLvl;

typedef struct {
    /* shape */
    int ngram, ctx_len, A, levels, max_length, ep_enabled;
    u32 num_contexts; /* A^(ctx_len)   */
    u64 cp_stride;    /* == A          */
    u64 drop_mod;     /* A^(ctx_len-1) */

    /* mmap'd level tables (read-only) */
    const u8 *ip, *cp, *ep;
    size_t ip_sz, cp_sz, ep_sz;

    /* from manifest */
    u8 *ln; /* length levels, max_length+1 entries */

    /* alphabet: per-code Unicode codepoint, for scoring an arbitrary password.
     * Alphabet entries are single Unicode characters (enforced by
     * Alphabet.from_chars), so one u32 codepoint per code is sufficient —
     * unlike omen_enum.c, which only ever needs the forward UTF-8 bytes for
     * *output*, this tool needs the reverse (bytes -> code) direction for
     * *input*, hence decoding to codepoints once at load time. */
    u32 *alpha_cp; /* per code: Unicode codepoint */

    /* derived bounds for pruning (identical role to omen_enum.c) */
    int min_cp, max_cp, min_ep, max_ep;

    /* IP contexts grouped by initial level (ctx ascending within a level) */
    u32 **ip_bucket;
    u32 *ip_bucket_cnt;

    /* lazily-built per-context continuation order, sorted by (level, code) */
    CodeLvl **cp_cache;

    /* counting-enumeration state */
    u64 count, limit; /* limit == 0 means unlimited */
    int stop;
} Model;

static void die(const char *msg) {
    fprintf(stderr, "omen-rank: %s\n", msg);
    exit(2);
}

static void die_errno(const char *msg) {
    fprintf(stderr, "omen-rank: %s: %s\n", msg, strerror(errno));
    exit(2);
}

/* base^exp with the same overflow guard as the Python model loader. */
static u64 checked_pow(u64 base, int exp, const char *what) {
    u64 r = 1;
    for (int i = 0; i < exp; i++) {
        if (base != 0 && r > MAX_TABLE_ENTRIES / base) die(what);
        r *= base;
    }
    if (r > MAX_TABLE_ENTRIES) die(what);
    return r;
}

static const u8 *map_table(const char *dir, const char *name, size_t *out_sz) {
    char path[4096];
    snprintf(path, sizeof(path), "%s/%s", dir, name);
    int fd = open(path, O_RDONLY);
    if (fd < 0) die_errno(path);
    struct stat st;
    if (fstat(fd, &st) != 0) die_errno(path);
    size_t sz = (size_t)st.st_size;
    const u8 *p = mmap(NULL, sz, PROT_READ, MAP_PRIVATE, fd, 0);
    close(fd);
    if (p == MAP_FAILED) die_errno("mmap");
    *out_sz = sz;
    return p;
}

/* Read the whole (small) manifest into a malloc'd buffer. */
static u8 *read_file(const char *dir, const char *name, size_t *out_sz) {
    char path[4096];
    snprintf(path, sizeof(path), "%s/%s", dir, name);
    FILE *f = fopen(path, "rb");
    if (!f) die_errno(path);
    if (fseek(f, 0, SEEK_END) != 0) die_errno(path);
    long sz = ftell(f);
    if (sz < 0) die_errno(path);
    rewind(f);
    u8 *buf = malloc((size_t)sz);
    if (!buf) die("out of memory reading manifest");
    if (fread(buf, 1, (size_t)sz, f) != (size_t)sz) die("short read on manifest");
    fclose(f);
    *out_sz = (size_t)sz;
    return buf;
}

static u32 rd_u32(const u8 *p) {
    u32 v;
    memcpy(&v, p, 4);
    return v; /* file is little-endian; assume LE host (x86-64 rig) */
}

/* Decode one UTF-8 sequence starting at p (NUL-terminated buffer, p < end).
 * Returns the codepoint and advances *p past it, or returns -1 (and leaves
 * *p unchanged) on malformed UTF-8 — the caller treats that as "unrepresentable",
 * same as Python's Alphabet.encode returning None for a foreign character. */
static long utf8_next(const u8 **p, const u8 *end) {
    const u8 *s = *p;
    if (s >= end) return -1;
    u8 b0 = s[0];
    int n;
    u32 cp;
    if (b0 < 0x80) {
        n = 1;
        cp = b0;
    } else if ((b0 & 0xE0) == 0xC0) {
        n = 2;
        cp = b0 & 0x1F;
    } else if ((b0 & 0xF0) == 0xE0) {
        n = 3;
        cp = b0 & 0x0F;
    } else if ((b0 & 0xF8) == 0xF0) {
        n = 4;
        cp = b0 & 0x07;
    } else {
        return -1;
    }
    if (s + n > end) return -1;
    for (int i = 1; i < n; i++) {
        u8 b = s[i];
        if ((b & 0xC0) != 0x80) return -1;
        cp = (cp << 6) | (b & 0x3F);
    }
    *p = s + n;
    return (long)cp;
}

/* Decode a NUL-terminated UTF-8 password into codepoints. Returns the
 * character count, or -1 on malformed UTF-8 / too long for MAX_PW_CHARS. */
static int decode_password(const char *pw, u32 *cps, int max_cps) {
    const u8 *p = (const u8 *)pw;
    const u8 *end = p + strlen(pw);
    int n = 0;
    while (p < end) {
        if (n >= max_cps) return -1;
        long cp = utf8_next(&p, end);
        if (cp < 0) return -1;
        cps[n++] = (u32)cp;
    }
    return n;
}

/* Reverse alphabet lookup: codepoint -> code, or -1 if out-of-alphabet.
 * Linear scan — A is at most 256 and this runs once per input character on
 * the (short) password being scored, not in the enumeration hot path. */
static int alphabet_code_of(const Model *m, u32 cp) {
    for (int c = 0; c < m->A; c++)
        if (m->alpha_cp[c] == cp) return c;
    return -1;
}

static void load_manifest(Model *m, const char *dir) {
    size_t sz;
    u8 *buf = read_file(dir, "manifest.bin", &sz);
    if (sz < 44) die("manifest too small");
    if (memcmp(buf, MANIFEST_MAGIC, 4) != 0) die("bad manifest magic");
    if (rd_u32(buf + 4) != FORMAT_VERSION) die("unsupported manifest version");
    m->ngram = (int)rd_u32(buf + 8);
    m->levels = (int)rd_u32(buf + 12);
    m->max_length = (int)rd_u32(buf + 16);
    m->ep_enabled = (int)rd_u32(buf + 20);
    m->A = (int)rd_u32(buf + 24);
    u32 ln_len = rd_u32(buf + 28);
    u32 alpha_bytes = rd_u32(buf + 32);
    /* lam at offset 36 (f64) is not needed for scoring; skip it. */

    if (m->ngram < 2 || m->ngram > 5) die("ngram out of range");
    if (m->levels < 2 || m->levels > 256) die("levels out of range");
    if (m->A < 1 || m->A > 256) die("alphabet size out of range");
    if ((int)ln_len != m->max_length + 1) die("ln length mismatch");

    size_t need = 44 + (size_t)m->A + alpha_bytes + ln_len;
    if (sz != need) die("manifest size mismatch");

    m->ctx_len = m->ngram - 1;
    m->num_contexts = (u32)checked_pow((u64)m->A, m->ctx_len, "ip/ep table too large");
    m->cp_stride = (u64)m->A;
    m->drop_mod = checked_pow((u64)m->A, m->ctx_len - 1, "context shift too large");

    const u8 *code_lens = buf + 44;
    const u8 *alpha = code_lens + m->A;
    const u8 *ln = alpha + alpha_bytes;

    m->ln = malloc(ln_len);
    m->alpha_cp = malloc(sizeof(u32) * (size_t)m->A);
    if (!m->ln || !m->alpha_cp) die("out of memory for manifest tables");
    memcpy(m->ln, ln, ln_len);

    /* Decode each alphabet entry's UTF-8 bytes to its single codepoint —
     * Alphabet.from_chars enforces exactly one character per entry, so each
     * decode must consume the entry's full byte length and nothing less. */
    const u8 *cursor = alpha;
    for (int c = 0; c < m->A; c++) {
        u8 clen = code_lens[c];
        const u8 *p = cursor;
        const u8 *entry_end = cursor + clen;
        long cp = utf8_next(&p, entry_end);
        if (cp < 0 || p != entry_end) die("malformed alphabet entry in manifest");
        m->alpha_cp[c] = (u32)cp;
        cursor = entry_end;
    }
    if ((u32)(cursor - alpha) != alpha_bytes) die("alphabet byte length mismatch");
    for (u32 i = 0; i < ln_len; i++)
        if (m->ln[i] > m->levels - 1) die("ln level out of range");
    free(buf);
}

static void load_tables(Model *m, const char *dir) {
    m->ip = map_table(dir, "ip.dat", &m->ip_sz);
    m->cp = map_table(dir, "cp.dat", &m->cp_sz);
    m->ep = map_table(dir, "ep.dat", &m->ep_sz);
    if (m->ip_sz != m->num_contexts) die("ip.dat size mismatch");
    if (m->ep_sz != m->num_contexts) die("ep.dat size mismatch");
    if (m->cp_sz != (size_t)m->num_contexts * (size_t)m->A) die("cp.dat size mismatch");
}

static void compute_bounds(Model *m) {
    int lo = 255, hi = 0;
    for (size_t i = 0; i < m->cp_sz; i++) {
        int v = m->cp[i];
        if (v < lo) lo = v;
        if (v > hi) hi = v;
    }
    m->min_cp = lo;
    m->max_cp = hi;
    if (m->ep_enabled) {
        lo = 255;
        hi = 0;
        for (u32 i = 0; i < m->num_contexts; i++) {
            int v = m->ep[i];
            if (v < lo) lo = v;
            if (v > hi) hi = v;
        }
        m->min_ep = lo;
        m->max_ep = hi;
    } else {
        m->min_ep = 0;
        m->max_ep = 0;
    }
}

static void build_ip_buckets(Model *m) {
    m->ip_bucket = calloc((size_t)m->levels, sizeof(u32 *));
    m->ip_bucket_cnt = calloc((size_t)m->levels, sizeof(u32));
    if (!m->ip_bucket || !m->ip_bucket_cnt) die("out of memory for ip buckets");
    for (u32 ctx = 0; ctx < m->num_contexts; ctx++) m->ip_bucket_cnt[m->ip[ctx]]++;
    u32 *fill = calloc((size_t)m->levels, sizeof(u32));
    if (!fill) die("out of memory for ip buckets");
    for (int l = 0; l < m->levels; l++) {
        if (m->ip_bucket_cnt[l]) {
            m->ip_bucket[l] = malloc(sizeof(u32) * m->ip_bucket_cnt[l]);
            if (!m->ip_bucket[l]) die("out of memory for ip buckets");
        }
    }
    for (u32 ctx = 0; ctx < m->num_contexts; ctx++) {
        u8 l = m->ip[ctx];
        m->ip_bucket[l][fill[l]++] = ctx;
    }
    free(fill);
}

static int codelvl_cmp(const void *a, const void *b) {
    const CodeLvl *x = a, *y = b;
    if (x->level != y->level) return (int)x->level - (int)y->level;
    return (int)x->code - (int)y->code;
}

static CodeLvl *cp_get(Model *m, u32 ctx) {
    CodeLvl *c = m->cp_cache[ctx];
    if (c) return c;
    c = malloc(sizeof(CodeLvl) * (size_t)m->A);
    if (!c) die("out of memory for cp cache");
    const u8 *row = m->cp + (size_t)ctx * (size_t)m->A;
    for (int code = 0; code < m->A; code++) {
        c[code].code = (u8)code;
        c[code].level = row[code];
    }
    qsort(c, (size_t)m->A, sizeof(CodeLvl), codelvl_cmp);
    m->cp_cache[ctx] = c;
    return c;
}

/* ---- scoring: the ip/cp/ep/ln walk, matching model.py's total_level() ---- */

typedef struct {
    int ok; /* 0 = unrepresentable */
    const char *reason;
    int total_level, length, ip_level, cp_sum, ep_level, ln_level;
} Score;

/* Scratch buffer for the length-rejection reason (interpolated, like
 * model.py's f"length {length} outside [{min_length}, {max_length}]"). One
 * password is scored at a time with its reason consumed before the next
 * call, so a single static buffer is safe — same simplicity level as the
 * rest of this file (no threads, no reentrancy). */
static char g_length_reason[96];

static Score score_password(Model *m, const char *pw) {
    u32 cps[MAX_PW_CHARS];
    int n = decode_password(pw, cps, MAX_PW_CHARS);
    if (n < 0) return (Score){.ok = 0, .reason = "contains an out-of-alphabet character"};

    u8 codes[MAX_PW_CHARS];
    for (int i = 0; i < n; i++) {
        int c = alphabet_code_of(m, cps[i]);
        if (c < 0) return (Score){.ok = 0, .reason = "contains an out-of-alphabet character"};
        codes[i] = (u8)c;
    }
    int length = n;
    if (length < m->ctx_len || length > m->max_length) {
        /* model.min_length is a property equal to ctx_len (model.py:155-157). */
        snprintf(
            g_length_reason, sizeof(g_length_reason), "length %d outside [%d, %d]", length,
            m->ctx_len, m->max_length
        );
        return (Score){.ok = 0, .reason = g_length_reason};
    }

    u32 ctx = 0;
    for (int i = 0; i < m->ctx_len; i++) ctx = ctx * (u32)m->A + codes[i];
    int ip = m->ip[ctx];
    int cp_sum = 0;
    for (int i = m->ctx_len; i < length; i++) {
        int code = codes[i];
        cp_sum += m->cp[(size_t)ctx * m->cp_stride + (size_t)code];
        ctx = (u32)((ctx % m->drop_mod) * (u64)m->A + (u64)code);
    }
    int ep = m->ep_enabled ? m->ep[ctx] : 0;
    int ln = m->ln[length];
    return (Score){
        .ok = 1,
        .total_level = ip + cp_sum + ep + ln,
        .length = length,
        .ip_level = ip,
        .cp_sum = cp_sum,
        .ep_level = ep,
        .ln_level = ln,
    };
}

/* ---- counting enumeration: identical shape to omen_enum.c's run(), but the
 * base case counts instead of formatting+writing a candidate. ---- */

static void count_recurse(Model *m, u32 ctx, int tl, int budget) {
    if (tl == 0) {
        int ep = m->ep_enabled ? m->ep[ctx] : 0;
        if (ep == budget) {
            m->count++;
            if (m->limit && m->count >= m->limit) m->stop = 1;
        }
        return;
    }
    int tt = tl - 1;
    int low = tt * m->min_cp + m->min_ep;
    int high = tt * m->max_cp + m->max_ep;
    CodeLvl *cc = cp_get(m, ctx);
    for (int i = 0; i < m->A; i++) {
        int level = cc[i].level;
        if (level > budget) break; /* sorted ascending: nothing further fits */
        int rem = budget - level;
        if (rem < low || rem > high) continue;
        u32 nctx = (u32)((ctx % m->drop_mod) * (u64)m->A + cc[i].code);
        count_recurse(m, nctx, tt, rem);
        if (m->stop) return;
    }
}

static void count_enumerate_length(Model *m, int length, int budget) {
    int transitions = length - m->ctx_len;
    int tail_low = transitions * m->min_cp + m->min_ep;
    int tail_high = transitions * m->max_cp + m->max_ep;
    int a_lo = budget - tail_high;
    if (a_lo < 0) a_lo = 0;
    int a_hi = budget - tail_low;
    if (a_hi > m->levels - 1) a_hi = m->levels - 1;
    for (int a = a_lo; a <= a_hi; a++) {
        u32 cnt = m->ip_bucket_cnt[a];
        if (!cnt) continue;
        u32 *ctxs = m->ip_bucket[a];
        int remaining = budget - a;
        for (u32 j = 0; j < cnt; j++) {
            count_recurse(m, ctxs[j], transitions, remaining);
            if (m->stop) return;
        }
    }
}

/* Count every candidate with total level in [0, max_level], across the
 * model's own length range — mirrors PyEnumerator.stream(max_level=...) with
 * no length filter, which is what PasswordScorer.estimate_rank relies on. */
static u64 count_rank(Model *m, int max_level, u64 cap) {
    m->count = 0;
    m->limit = cap;
    m->stop = 0;
    int lo = m->ctx_len, hi = m->max_length;
    for (int total = 0; total <= max_level; total++) {
        for (int length = lo; length <= hi; length++) {
            int budget = total - m->ln[length];
            if (budget < 0) continue;
            count_enumerate_length(m, length, budget);
            if (m->stop) return m->count;
        }
    }
    return m->count;
}

/* ---- CLI ---- */

static u64 parse_u64(const char *s, const char *flag) {
    errno = 0;
    char *end = NULL;
    unsigned long long v = strtoull(s, &end, 10);
    if (errno != 0 || end == s || *end != '\0') {
        fprintf(stderr, "omen-rank: invalid value for %s: %s\n", flag, s);
        exit(2);
    }
    return (u64)v;
}

static void report(Model *m, const char *pw, u64 rank_cap) {
    Score s = score_password(m, pw);
    if (!s.ok) {
        printf("%s\tUNREACHABLE\t(%s)\n", pw, s.reason);
        return;
    }
    u64 rank = 0;
    if (s.total_level > 0) rank = count_rank(m, s.total_level - 1, rank_cap);
    printf(
        "%s\tlevel=%d\tlen=%d\tip=%d cp=%d ep=%d ln=%d\trank>=%" PRIu64 "\n",
        pw, s.total_level, s.length, s.ip_level, s.cp_sum, s.ep_level, s.ln_level, rank
    );
}

static void report_from_stream(Model *m, FILE *f, u64 rank_cap) {
    char *line = NULL;
    size_t cap = 0;
    ssize_t len;
    while ((len = getline(&line, &cap, f)) != -1) {
        while (len > 0 && (line[len - 1] == '\n' || line[len - 1] == '\r')) line[--len] = '\0';
        if (len == 0) continue;
        report(m, line, rank_cap);
    }
    free(line);
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: omen-rank <model_dir> [-i FILE] [--rank-cap N] [password ...]\n");
        return 2;
    }
    const char *dir = argv[1];
    const char *input_file = NULL;
    u64 rank_cap = DEFAULT_RANK_CAP;
    const char *passwords[256];
    int n_passwords = 0;

    int i = 2;
    while (i < argc) {
        if (strcmp(argv[i], "-i") == 0) {
            if (++i >= argc) die("-i requires a value");
            input_file = argv[i++];
        } else if (strcmp(argv[i], "--rank-cap") == 0) {
            if (++i >= argc) die("--rank-cap requires a value");
            rank_cap = parse_u64(argv[i++], "--rank-cap");
        } else {
            if (n_passwords >= (int)(sizeof(passwords) / sizeof(passwords[0])))
                die("too many positional passwords (use -i FILE for bulk input)");
            passwords[n_passwords++] = argv[i++];
        }
    }
    if (!input_file && n_passwords == 0) die("no passwords given (use -i FILE or positional args)");

    Model m;
    memset(&m, 0, sizeof(m));
    load_manifest(&m, dir);
    load_tables(&m, dir);
    compute_bounds(&m);
    build_ip_buckets(&m);
    m.cp_cache = calloc((size_t)m.num_contexts, sizeof(CodeLvl *));
    if (!m.cp_cache) die("out of memory for cp cache index");

    for (int k = 0; k < n_passwords; k++) report(&m, passwords[k], rank_cap);

    if (input_file) {
        FILE *f = strcmp(input_file, "-") == 0 ? stdin : fopen(input_file, "r");
        if (!f) die_errno(input_file);
        report_from_stream(&m, f, rank_cap);
        if (f != stdin) fclose(f);
    }
    return 0;
}
