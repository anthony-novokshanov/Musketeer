[{{requester_id}}] just received this new task:
{{task_summary}}

Skills the task needs (weight 0-1): {{required_skills}}

Candidates, each with task_relevance (0-1: how recently and repeatedly they used those skills)
and their expertise card:
{{candidates}}

Find up to 3 of these candidates who have done this kind of work before and could help [{{requester_id}}] with it.
Here "these two people" means the requester and the candidate, judged on this task.

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

Start each reason with a past-tense verb describing what the candidate did, without their
name (e.g. "Ran MLH booths at three events this year."). It is shown after their name.

Return JSON: candidates, up to 3 entries ordered by score descending, each
{person_id, score, reason}. Only use candidate ids. Never include {{requester_id}}.
