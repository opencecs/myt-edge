# MVX poll wakeup regression test

Run from a Linux development host with Python 3, a C compiler available as
cc, and pthread support:

    python3 -B scripts/validation/mvx_poll/test_mvx_poll.py --kernel-source /path/to/cix-kernel

--kernel-source is required; the test never assumes which checkout to use.
Generated C and executables live in a temporary directory and are removed
after the run. The test does not access a board, build a kernel, or create a
firmware image.

The harness extracts the actual mvx_v4l2_poll() and mvx_v4l2_buffer_done()
bodies from the specified checkout. A small userspace vb2 model uses real
pthread, epoll and eventfd operations to reproduce the first-call wait
registration boundary. The negative controls keep the old completion
notification and must lose the late CAPTURE or session-error wakeup.
The fixed paths must wake without inventing readiness.

The 10 cases cover old late-allocation failure, old already-streaming success,
late CAPTURE allocation, allocated-but-not-streaming CAPTURE, input/output
DONE and ERROR completion, QUEUED rollback without false completion, and
session errors before allocation. An additional source check ensures all
direct vb2_buffer_done() calls use the shared helper.

This is a regression model, not an in-kernel or firmware execution test.
A passing result still requires compilation and device validation without
the diagnostic preload, including playback, seek and repeated open/close.
