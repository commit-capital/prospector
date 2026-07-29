// `pnpm -s test` runs this inside a phase container, which has no egress. The
// require() is the assertion: `ms` resolves only from the node_modules the Tier 1
// image build installed offline and hardlinked out of a store it then deleted.
const ms = require("ms");

if (ms("2s") !== 2000) {
  console.error(`ms resolved but returned ${ms("2s")}`);
  process.exit(1);
}
console.log("TIER1_DEP_OK");
