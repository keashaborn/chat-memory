# Memory Routing Map

Current checkpoint: 079d258 Add FM conceptual route

## Active Vantage path

vantage_router.py:
- classifies turn intent
- builds retrieval plan
- calls personal/corpus retrieval
- combines memory chunks
- builds debug metadata
- calls prompt_builder.build_system_prompt()

retriever_unified.py:
- performs corpus/vector retrieval

prompt_builder.py:
- assembles final system prompt
- gates profile cards
- formats retrieved memory
- compacts memory for MEMORY_ARCHITECTURE and FM_CONCEPTUAL

memory_route_audit.py:
- regression harness for routing, prompt injection, store gating, and debug metadata

## Current turn intents

TECH:
- suppress FM lens
- suppress personal archive
- suppress corpus
- suppress profile cards

MEMORY_ARCHITECTURE:
- suppress FM lens
- limited personal archive
- limited corpus
- suppress profile cards
- enable debug semantic previews

FM_CONCEPTUAL:
- allow FM lens
- allow conceptual corpus
- suppress broad personal archive
- suppress profile cards
- compact retrieved memory

SPECIFIC_RECALL:
- prioritize personal archive
- suppress corpus
- suppress profile cards
- allow raw answer-bearing personal memory

PROFILE_SUMMARY:
- allow profile cards
- allow personal archive
- allow corpus

## Prompt inspector principle

The final system prompt is the runtime phenotype of the memory system.

The main question is always:

- what was injected
- what was suppressed
- how much context was injected
- which store supplied it
- whether the final answer had the right behavioral conditions
