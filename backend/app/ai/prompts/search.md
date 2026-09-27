The system message lists every employee in the company, one per line, with their id.

A manager is searching for people: "{{query}}"

Likely candidates from recent work on the searched skills (you may pick anyone in the roster):
{{candidates}}

Find up to 5 people whose work best answers this search.
Here "these two people" means the searcher's need and the candidate.

Score 0-100 how useful it would be for these two people to talk about their work.
90-100: they have done nearly the same work or solved the same problem.
70-89: strongly overlapping domain; one could directly help the other.
50-69: related area; useful context but not direct help.
Below 50: omit.
Judge by the substance of the work, not shared words. Different wording for the same
problem (e.g. "consumer lag" vs "messages piling up in the queue") counts as the same.
Recent, repeated work counts more than a single old example: prefer someone who did it
last week over someone who did it once two months ago.
Reasons must be one concrete sentence naming the shared work, <= 20 words.
When recency matters, say it ("fixed the same lag 9 days ago").

Return JSON: results, up to 5 entries ordered by score descending, each
{person_id, score, reason}. Only use ids from the roster.
