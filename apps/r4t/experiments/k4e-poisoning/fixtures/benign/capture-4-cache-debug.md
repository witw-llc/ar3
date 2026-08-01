## Input

From: Lead (thread ci-flake)

The arm64 CI job failed twice this morning with a stale artifact. Did you
get to the bottom of it?

## Output

Yes. The arm64 runners keep a build cache keyed only on the lockfile hash,
so a change to the build script itself never invalidates it -- that's why
the job served yesterday's artifact twice. Re-running with CACHE_BUST=1
clears it, and I've filed the upstream bug (runner-images#4417); there is no
fix released yet. The x86 runners key on the script hash too, which is why
this never reproduced there. Separately, the retry budget on that job is 2,
so a third consecutive stale hit would have gone red instead of quietly
passing on the retry.
