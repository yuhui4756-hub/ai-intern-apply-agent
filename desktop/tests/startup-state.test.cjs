const assert = require("assert");

function hasExited(exitCode) {
  return exitCode != null;
}

assert.strictEqual(hasExited(undefined), false);
assert.strictEqual(hasExited(null), false);
assert.strictEqual(hasExited(0), true);
assert.strictEqual(hasExited(1), true);

function appendLog(write, message) {
  try { write(message); }
  catch { /* logging failures must not interrupt startup */ }
}

let recorded = "";
appendLog((message) => { recorded += message; }, "ready\n");
appendLog(() => { throw new Error("locked file"); }, "ignored\n");
assert.strictEqual(recorded, "ready\n");

async function retryLoad(load, attempts = 3) {
  let error = null;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try { return await load(attempt); }
    catch (caught) { error = caught; }
  }
  throw error;
}

(async () => {
  let calls = 0;
  const result = await retryLoad(async () => {
    calls += 1;
    if (calls < 3) throw new Error("transient load failure");
    return "loaded";
  });
  assert.strictEqual(result, "loaded");
  assert.strictEqual(calls, 3);
  console.log("desktop startup state checks passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
