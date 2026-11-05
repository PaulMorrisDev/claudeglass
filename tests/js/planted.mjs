// The conversation the compaction review tests plant: constraints, a decision,
// open tasks, edited files and a failing test.

/** The planted conversation: constraints, a decision, edited files and a failing test. */
export function planted() {
  return [
    {
      role: "user",
      text: "Never use port 8765 for the dev server, it is reserved. Don't read any code yet; wait for the other session to finish. Please add rate limiting to the API.",
    },
    {
      role: "assistant",
      text: "Here is a plan.",
      toolUses: [
        {
          tool: "TodoWrite",
          input: {
            todos: [
              { content: "Write the limiter", status: "completed" },
              { content: "Add the burst test", status: "in_progress" },
              { content: "Document the header", status: "pending" },
            ],
          },
        },
      ],
    },
    { role: "user", text: "Let's go with option B, the queue-based design. Use pnpm, not npm." },
    {
      role: "assistant",
      text: "Editing.",
      toolUses: [
        { tool: "Edit", input: { file_path: "src/api/limiter.ts" }, text: "ok" },
        { tool: "Write", input: { file_path: "src/api/queue.ts" }, text: "ok" },
        { tool: "Edit", input: { file_path: "tests/test_limiter.py" }, text: "ok" },
        { tool: "Edit", input: { file_path: "src/api/limiter.ts" }, text: "ok" },
        { tool: "Edit", input: { file_path: "docs/limits.md" }, text: "ok" },
        { tool: "Edit", input: { file_path: "src/config.ts" }, text: "ok" },
        { tool: "NotebookEdit", input: { notebook_path: "notes/try.ipynb" }, text: "ok" },
      ],
    },
    {
      role: "assistant",
      text: "Running tests.",
      toolUses: [
        {
          tool: "Bash",
          input: { command: "pytest tests/test_limiter.py" },
          isError: true,
          text: "collected 3 items\nFAILED tests/test_limiter.py::test_burst - AssertionError: expected 429\n1 failed, 2 passed",
        },
      ],
    },
  ];
}
