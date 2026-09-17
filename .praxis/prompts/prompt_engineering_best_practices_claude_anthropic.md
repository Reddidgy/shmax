# Prompt Engineering Best Practices for Claude (Anthropic)

A comprehensive compilation sourced from Anthropic's official documentation, AWS guides, and community-verified resources.

---

## 1. The Golden Rule: Clarity Above All

Anthropic's #1 tip (from their AWS re:Invent 2025 talk):

> "Show your prompt to a colleague with minimal context on the task and ask them to follow it. If they'd be confused, Claude will be too."

Think of Claude as an intern on their first day  -  provide clear, explicit instructions with all necessary detail. Be specific about the desired output format and constraints.

---

## 2. Give Claude a Role (System Prompt)

Setting a role in the system prompt focuses Claude's behavior and tone. Even a single sentence makes a difference:

---

## 3. Think Step by Step

Telling Claude to "think step by step" often dramatically improves accuracy. Anthropic's research team confirms that giving Claude time to think through its response before producing the final answer leads to better performance.

- Include "Think step by step" in your prompt
- Capture step-by-step thought process by instructing: "please think about it step-by-step in a dedicated thinking section"
- Use a "scratchpad" approach for complex reasoning tasks

---

## 4. Use Extended Thinking / Adaptive Thinking

Extended thinking gives Claude a dedicated scratchpad of "thinking tokens" that happen before the public response. These tokens aren't part of the billable output shown to users.

- Extended thinking is an API-level parameter (not just a prompt instruction)
- Adaptive thinking (Claude Opus 4.6+, Sonnet 4.6+) lets Claude dynamically decide how many thinking tokens each request warrants
- Prefer general instructions ("think thoroughly") over prescriptive steps  -  Claude's reasoning frequently exceeds what a human would prescribe
- Multishot examples work with thinking: include few-shot examples that show Claude the reasoning pattern

---

## 5. Provide Examples (Few-Shot Prompting)

- Give examples of your desired output as part of your prompt
- Include both good and edge-case examples
- Include thinking examples to demonstrate the reasoning pattern
- Even unrelated examples can help establish format expectations

---

## 6. Prefill Claude's Response (Assistant Turn Prefilling)

Use the assistant message to provide the beginning of the output. This prevents Claude from being chatty and enforces specific formats:

- For JSON output: `{"role": "assistant", "content": "{"}`
- For role-play: prefill with a bracketed role name to maintain character
- Skips preambles and introductory text

---

## 7. Long Context Prompting (20k+ tokens)

- Put longform data at the top: Place long documents and inputs near the top of your prompt, above your query, instructions, and examples  -  this significantly improves performance
- Use structured sections to organize multiple documents with dedicated content and source sections
- Anthropic tested with documents of 75,000–90,000 tokens and found structured prompting critical for recall accuracy

---

## 8. Prompt Chaining (Break Complex Tasks into Steps)

For complex tasks, break them into multiple prompts corresponding to each step:

- Each new prompt can include previous prompt-response pairs to build context
- Identify subtasks: each step should have a single, clear objective
- Use structured formats for handoffs between steps
- Particularly useful for research synthesis, document analysis, or iterative content creation
- If a specific step doesn't perform well, isolate and fine-tune it without reworking the entire task

---

## 9. Tell Claude What TO Do (Not What NOT to Do)

- Instead of: "Do not use markdown in your response"
- Try: "Your response should be composed of smoothly flowing prose paragraphs"
- Match your prompt style to the desired output style

---

## 10. Allow Claude to Say "I Don't Know"

Explicitly instruct Claude to say "I don't know" if it's unsure. This is critical for RAG applications to minimize hallucinations.

---

## 11. Provide Context and Motivation

Explaining why certain behavior is important helps Claude better understand your goals:

- Add context behind your instructions
- Claude is smart enough to generalize from explanations
- Subject matter expert (SME) guidance embedded in prompts significantly improves domain-specific accuracy

---

## 12. Multi-Context-Window Tasks

For tasks spanning multiple context windows:

1. Use the first context window to set up a framework (write tests, create setup scripts)
2. Use future context windows to iterate on a todo-list
3. Have the model write tests in a structured format (e.g., `tests.json`) for better long-term iteration
4. Remind Claude of the importance of tests to prevent functionality loss

---

## 13. Steer Thinking Behavior

You can prompt Claude to control when and how it uses extended thinking:

- To encourage reflection: "After receiving tool results, carefully reflect on their quality and determine optimal next steps before proceeding."
- To reduce unnecessary thinking: "Extended thinking adds latency and should only be used when it will meaningfully improve answer quality  -  typically for problems that require multi-step reasoning. When in doubt, respond directly."
