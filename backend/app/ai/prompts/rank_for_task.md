The system message lists every employee in the company, one per line, with their id.

[{{requester_id}}] just received this new task:
{{task_summary}}

Find up to 3 colleagues who have done this kind of work before and could help [{{requester_id}}] with it.
Here "these two people" means the requester and the candidate, judged on this task.

Score 0-100 how useful it would be for these two people to talk about their work.
90-100: they have done nearly the same work or solved the same problem.
70-89: strongly overlapping domain; one could directly help the other.
50-69: related area; useful context but not direct help.
Below 50: omit.
Judge by the substance of the work, not shared words. Different wording for the same
problem (e.g. "consumer lag" vs "messages piling up in the queue") counts as the same.
Reasons must be one concrete sentence naming the shared work, <= 20 words.

Start each reason with a past-tense verb describing what the candidate did, without their
name (e.g. "Ran MLH booths at three events this year."). It is shown after their name.

Return JSON: candidates, up to 3 entries ordered by score descending, each
{person_id, score, reason}. Only use ids from the roster. Never include {{requester_id}}.
