## Inspiration

We initially recognized that connections within companies are often neglected from both the employee and the management side. The connections society usually emphasizes are family and friendships, but one that often gets left in the dust is work relationships, even though around a third of your life after college is spent working. Along with neglected workplace relationships comes another issue that each of us has seen at our internships: **knowledge gaps between teams and employees**. We needed a way to fix this, and that's where we came to our solution.

## What it does

**Musketeer automatically connects employees who do similar work but don't know each other.**

Say you're an intern who just got a new task and doesn't know who to ask. When the task arrives by email or Slack message, Musketeer detects it, figures out the skills it needs, and finds the coworker who recently solved the same problem. You get a Slack message (or a message in whatever service your company uses) like:

> *"Alice ran an MLH booth 9 days ago. Want to connect?"*

If you both agree, Musketeer opens a chat and facilitates the connection, from the online conversation to setting up a meeting.

It's not another AI that hands you an answer; it connects you with a person who can show you **how** they solved it. Employees never leave the platforms they already use.

Managers of each team get a dashboard with:

- **Company map:** a zoomable view showing who could help whom and which teams should be talking
- **Alerts:** flags for people stuck without a connection, plus one-click nudges
- **Auto group:** builds a team from a plain-English project description
- **Synopsis:** tracks connections made and how helpful they were

## How we built it

- **Meta Muse Spark** (Meta Model API): task detection, skill extraction, skill canonicalization, match judging, and writing reasons, icebreakers, and summaries. It runs across 17 structured-JSON prompts with response caching.
- **Meta FAIR Contriever:** semantic retrieval to narrow candidates at scale (hybrid mode)
- **Tiger Cloud Postgres + TimescaleDB:** hypertables store the time-stamped expertise graph, continuous aggregates power the analytics, and pgvector stores embeddings with pgvectorscale StreamingDiskANN indexes
- **Decay model (SQL + NumPy):** computes current skill levels with a 30-day half-life and compares people with cosine similarity, refreshing every 10 minutes with no AI calls
- **Python + FastAPI:** backend, the matching pipeline, and background jobs
- **Slack Bolt (Socket Mode) + Block Kit:** notifications, group chats, meeting scheduling, shared meeting notes, and feedback buttons
- **Gmail API:** incoming email for task detection
- **Google Calendar API:** finds a time both people are free and books the meeting with a Google Meet link
- **JavaScript + D3:** zoomable company graph and analytics in a single-page dashboard

## Improvements: scaling with Meta's embedding models

Our first version sent every employee's profile to Muse. That works for 40 people, but **at 10,000 it's ~2.2M tokens**, past Muse's context window.

So we added a hybrid mode with **Meta FAIR Contriever**:

- **Embeddings:** profiles, past work, tasks, and searches become vectors in pgvector.
- **Indexes:** pgvectorscale StreamingDiskANN keeps search fast as the company grows.
- **Retrieval:** the closest recent work adds up to 10 people to our decay model's picks.
- **Judging:** Muse only sees that shortlist (~7,000 tokens), no matter the company size.

To test it, we wrote a benchmark: synthetic vectors on our Tiger Cloud database, 50 queries, index vs. scanning every row.

| People | Index (median) | Exact scan (median) | Speedup | Recall@10 |
| --- | --- | --- | --- | --- |
| 2,000 | 3.1 ms | 5.9 ms | 1.9x | 1.000 |
| 50,000 | 6.3 ms | 273 ms | 43x | 0.986 |

The vectors are synthetic, so this measures speed, not match quality. On our real data, hybrid still picks the right person.

## Challenges we ran into

- **Keeping experts from getting spammed:** Judges emphasized that experts shouldn't be flooded with requests, so we added load balancing that spreads requests across qualified people.
- **Synonym merging:** Getting Muse to merge synonyms without collapsing unrelated skills took a lot of prompt iteration.
- **Tuning decay and weights:** Recent experts needed to win without old expertise vanishing.
- **No real company data:** We generated a realistic 40-person org with planted test cases to prove the model works.
- **Demo reliability:** Live AI, Slack, and Gmail all have to work on camera. We solved it with caching, simulated delivery, and a backup trigger route.

## Accomplishments that we're proud of

- **A full live loop:** an email arrives, a Slack match goes out in seconds, and the manager's dashboard updates live.
- **Recency beats stale expertise:** a recent expert outranks one with a single fix 55 days old.
- **New skills show up fast:** they appear in matches within minutes, with no extra AI calls.
- **Built entirely on Meta's AI stack.**

## What we learned

- **Use AI for judgment and math for memory.** The split made the system faster, cheaper, and more explainable.
- **Expertise has a timestamp.** A static profile is outdated the day it's written.
- **Sometimes the best answer is a person, not a chatbot.**

## What's next for Musketeer

- Real GitHub and Jira integrations
- Learned connection prediction from the helpful/not helpful ratings we already collect
- Temporal graph neural networks for predicting useful connections
- Scoped manager views and employee opt-in controls
