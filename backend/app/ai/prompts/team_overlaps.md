Below are the profiles of every team in the company.

{{teams}}

Find pairs of teams whose work overlaps enough that their leads should talk.
Score 0-100: 90+ the teams do nearly the same work; 70-89 strongly overlapping work where
one team could directly help the other; 50-69 related areas. Omit pairs below 50.
Judge by the substance of the work, not shared words.

Return JSON: pairs, each {team_a, team_b, score, shared_topics, summary}.
- team_a and team_b are team ids from the list; put the alphabetically smaller id in team_a.
- shared_topics: up to 5 lowercase topics.
- summary: <= 40 words naming the overlapping work.
