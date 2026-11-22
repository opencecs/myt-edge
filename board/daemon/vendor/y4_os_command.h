#ifndef Y4_OS_COMMAND_H
#define Y4_OS_COMMAND_H
#include <stddef.h>
#include <stdint.h>
/* In-process descriptor bound; RPC routes remain path/verb strings. */
#define Y4_COMMAND_MAX 512u
#define Y4_FIELDS_MAX 64u
#define Y4_COMMAND_TEXT_MAX 65536u
#define Y4_COMMAND_TEXT_TOTAL_MAX 65536u
#define Y4_COMMAND_TIMEOUT_MAX 600000u
enum y4_value_type { Y4_BOOL = 1,Y4_I64,Y4_U64,Y4_TEXT };
struct y4_text { const char *data; size_t length; };
struct y4_value {
    const char *name;
    enum y4_value_type type;
    union { int boolean; int64_t signed_integer; uint64_t unsigned_integer; struct y4_text text; } data;
};
struct y4_field {
    const char *name;
    enum y4_value_type type;
    int required;
    union { struct { int64_t minimum,maximum; } signed_integer;
            struct { uint64_t minimum,maximum; } unsigned_integer;
            struct { size_t minimum,maximum; } text; } range;
};
struct y4_schema { const struct y4_field *fields; size_t count; };
enum y4_http_method { Y4_GET = 1,Y4_POST,Y4_PATCH,Y4_DELETE };
enum y4_idempotency { Y4_IDEMPOTENCY_NONE = 0,Y4_IDEMPOTENCY_REQUIRED };
enum y4_execution { Y4_SYNCHRONOUS = 0,Y4_PERSISTENT_TASK,Y4_DURABLE_OBJECT,Y4_SUPERVISOR_REQUEST };
struct y4_call {
    void *opaque;
    uint64_t (*now_ms)(void *);
    int (*cancelled)(void *);
    uint64_t started_ms,deadline_ms;
};
typedef int (*y4_command_handler)(const struct y4_call *,const struct y4_value *,size_t,
                                  struct y4_value *,size_t,size_t *);
struct y4_command {
    const char *path,*verb,*rest_path,*help;
    uint32_t schema_version,timeout_ms;
    uint64_t required_permissions;
    enum y4_http_method method;
    enum y4_idempotency idempotency;
    enum y4_execution execution;
    int mutating;
    struct y4_schema input,output;
    y4_command_handler handler;
};
struct y4_command_registry { const struct y4_command *commands; size_t count; };
struct y4_command_actor { int authenticated; uint64_t permissions; };
struct y4_dispatch_context { void *opaque; uint64_t (*now_ms)(void *); int (*cancelled)(void *); };
enum y4_dispatch_stage { Y4_STAGE_NONE = 0,Y4_STAGE_AUTHORIZATION,Y4_STAGE_ROUTE,Y4_STAGE_REQUEST,
                         Y4_STAGE_DISPATCH,Y4_STAGE_EXECUTION,Y4_STAGE_RESULT,Y4_STAGE_RESPONSE,Y4_STAGE_COMPLETE };
struct y4_dispatch_result {
    size_t output_count;
    int handler_invoked,completed_after_deadline,status,handler_status;
    enum y4_dispatch_stage stage;
    /* Trusted limiter hint only: execution EAGAIN, 1..60000ms. Zero keeps
     * ordinary transient-error semantics; never accepts a client retry claim. */
    uint64_t retry_after_ms;
};
/* Registry and descriptor/schema strings are immutable and caller-owned for the
 * registry lifetime. No allocation, threads or global state. Read-only concurrent
 * dispatch permitted if handlers/context support it. Actor is a trusted product
 * authorization result, never a client-provided bitmask. Schema uses strict scalar
 * values, no coercion/unknown/duplicate fields, bounded UTF-8 text (no NUL).
 * Input/output/context/result storage must not overlap. Output values belong
 * to caller/handler; text must survive return. */
/* Handlers access validated fields by name, never depend on input order. */
const struct y4_value *y4_value_find(const struct y4_value *,size_t,const char *);
int y4_command_route_validate(const char *,const char *);
int y4_schema_validate(const struct y4_schema *,const struct y4_value *,size_t);
int y4_command_registry_init(struct y4_command_registry *,const struct y4_command *,size_t);
const struct y4_command *y4_command_find(const struct y4_command_registry *,const char *,const char *);
/* Cooperative cancellation/deadline; does not stop a handler or undo side effects.
 * Handlers poll at bounded safe points. Successful late completion remains actual
 * success with completed_after_deadline=1; never fabricate timeout rollback. */
int y4_command_poll(const struct y4_call *);
/* Typed backend only: no JSON parser/HTTP server/CLI front end yet. Mutating and
 * persistent-task dispatch fails EOPNOTSUPP until durable idempotency/task layer
 * exists; handler is not invoked. Errors clear output records/count. */
int y4_command_dispatch(const struct y4_command_registry *,const char *,const char *,
                         const struct y4_command_actor *,const struct y4_value *,size_t,
                         const struct y4_dispatch_context *,struct y4_value *,size_t,
                         struct y4_dispatch_result *);
/* Explicit trusted admission for mutating PERSISTENT_TASK/DURABLE_OBJECT
 * descriptors with required idempotency. Ordinary dispatch still rejects them.
 * Admission persists the task or immutable object and returns its identity,
 * enforcing idempotency without device effects. Durable objects are not tasks.
 * Callback gets selected descriptor, validated input and deadline. Caller
 * supplies trusted authenticated context. Errors after callback may mean an
 * object/task exists; never infer rollback or blindly retry. */
typedef int (*y4_command_admission)(const struct y4_command *,const struct y4_call *,
 const struct y4_value *,size_t,struct y4_value *,size_t,size_t *);
int y4_command_dispatch_admitted(const struct y4_command_registry *,const char *,const char *,
 const struct y4_command_actor *,const struct y4_value *,size_t,const struct y4_dispatch_context *,
 struct y4_value *,size_t,struct y4_dispatch_result *,y4_command_admission);
#endif
