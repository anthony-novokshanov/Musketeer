The system message lists every employee in the company, one per line, with their id.

Find the people whose work overlaps most with [{{target_id}}].

Score 0-100 how useful it would be for these two people to talk about their work.
90-100: they have done nearly the same work or solved the same problem.
70-89: strongly overlapping domain; one could directly help the other.
50-69: related area; useful context but not direct help.
Below 50: omit.
Judge by the substance of the work, not shared words. Different wording for the same
problem (e.g. "consumer lag" vs "messages piling up in the queue") counts as the same.
Reasons must be one concrete sentence naming the shared work, <= 20 words.

Return JSON: matches, at least 3 and up to {{topk}} entries (fewer only if fewer people score
50 or more), ordered by score descending, each
{person_id, score, reason}. Only use ids from the roster. Never include {{target_id}}.
