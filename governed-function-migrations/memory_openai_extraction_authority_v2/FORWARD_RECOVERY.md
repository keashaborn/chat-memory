# Forward recovery

Stop the inactive OpenAI extraction worker, verify the exact owner/job/content binding and zero provider activity, apply the exact rollback function in one transaction, then reapply only after operator review. No queue, evidence, Qdrant, or provider mutation is permitted during recovery.
