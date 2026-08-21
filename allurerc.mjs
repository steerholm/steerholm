// Allure 3 report configuration.
// Plain object (defineConfig is optional typing sugar) so it needs no import
// resolution when run via `npx allure`.
export default {
  name: "MCP Harbour Tests",
  output: "allure-report",
  // History is read from and written back to this file; CI persists it across
  // runs (via cache) to build trend charts.
  historyPath: "history.jsonl",
  plugins: {
    awesome: {
      options: {
        reportName: "MCP Harbour Tests",
      },
    },
  },
};
