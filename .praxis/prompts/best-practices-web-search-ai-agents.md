# Best Practices for Web Search for AI Agents

## 1. Introduction

Web search is a critical skill for modern AI agents. Without it, an agent is limited by its training data cutoff. With it, an agent can find up-to-date information, verify facts, compare products, track changes, and perform deep research.

However, simply giving an agent access to a search API is not enough. Anthropic research shows: the difference between an agent that "just searches" and an agent that *researches* is up to 90% in answer quality. This difference comes from the right search loop architecture.

---

## 2. Fundamental Principles of the Search Loop

Effective web search is not a single query  -  it's a control loop, not a pipeline:


Intent Analysis → Query Decomposition → Query Construction → Search → Result Evaluation → [Enough?] → Synthesis (↻ (iterate))
The agent must decide: continue (gaps remain), change strategy (query is ineffective), or stop (enough evidence gathered).

### Key Decisions Per Iteration:
- Continue  -  are there gaps worth filling?
- Switch strategy  -  does the approach need to change?
- Stop  -  is there enough evidence to answer?

---

## 3. Intent Analysis and Decomposition

The first and most important step is understanding what the user actually needs. AIQuinta identifies 5 key questions a query planner should ask:

1. What does the user need to know?
2. What evidence would confirm the answer?
3. What sources likely contain that evidence?
4. What query structure will retrieve those sources?
5. Is enough reliable information found?

### Query Decomposition

Instead of one broad query, the agent breaks the question into sub-questions. Example: for "How does an AI agent plan search queries?":

- What is query planning?
- How does intent determination work?
- How are queries generated?
- How do search operators improve results?
- How are results verified and refined?

### Intent Classification

Determine the answer type: fact, comparison, update, technical explanation, source, process. This determines the search strategy.

| Pros | Cons |
|------|------|
| Reduces noise by 40% (SearchCans) | Adds latency (1-2 extra LLM calls) |
| Improves result relevance | Requires quality prompt engineering |
| Saves tokens and time | Can create a bottleneck with poor classification |

---

## 4. Query Formulation and Optimization Techniques

### 4.1. Search Operators

| Operator | Purpose | Example |
|----------|---------|---------|
| `"exact phrase"` | Search exact phrase | `"query planning" "AI agent"` |
| `OR` | Include related terms | `"AI search agent" OR "agentic search"` |
| `site:` | Search within trusted domain | `site:arxiv.org "query planning"` |
| `filetype:` | Find PDFs, DOCs, etc. | `filetype:pdf "AI agent"` |
| `after:` | Date filter | `after:2025 "AI search agent"` |
| `intitle:` | Word in page title | `intitle:"query planning"` |
| `-exclude` | Exclude irrelevant | `"web search agent" -job -course` |

### 4.2. LLM Query Optimization

Data4AI suggests using LLM to reformulate raw user input into optimized search queries:

```
User: "Plan a 3-day trip to Kyoto. I want hidden gems, not tourist places."
→ LLM reformulates: "hidden gems Kyoto off the beaten path 3-day itinerary local guide blog"
→ Search API returns niche blogs and forums instead of "Top 10" articles
```
### 4.4. Behavioral Optimization

The system learns from clicks and repeat queries: if users searching "freelance tax advice" click more on "self-employment deductions", the system prioritizes those terms.

| LLM vs Rule-based | |
|--------------------|---|
| + LLM is more flexible, understands context | − LLM can hallucinate filters |
| + Rule-based is faster, cheaper, predictable | − Rule-based requires manual maintenance |
| + Optimal: combine both approaches | − Behavioral approach needs lots of data |

---
### 4.5. Two-Tool Separation

Claude Code uses two separate tools:
- WebSearch  -  returns only titles and URLs (cheap)
- WebFetch  -  takes URL + prompt, Claude Haiku extracts only the needed answer (not full HTML)

Search stays cheap. Deep reading doesn't enter context until needed.

Tip: Use separate limits: 10-15 searches per 3-5 deep reads.
