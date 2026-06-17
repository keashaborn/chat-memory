# Verbal Sage Product Architecture Roadmap

_Last updated: 2026-06-16_

## Purpose

This document defines the long-range product architecture for Verbal Sage / SeeBx so that near-term engineering decisions do not accidentally trap the system inside the current developer cockpit.

The current system already contains powerful Vantage controls, memory pipelines, prompt injection, corpus retrieval, and Vantage-scoped learning. That power should be preserved. The next step is to separate it into usable product areas so the system can support ordinary users, advanced/admin use, character/roleplay systems, health tracking, and future creative media without those domains contaminating each other.

## Core Principle

Verbal Sage should be organized around durable identities and bounded contexts, not loose presets.

A Vantage is a durable perspective, identity, memory namespace, and behavior profile.

A preset or mode is an adjustable configuration inside a Vantage.

A room is a shared interaction space where one or more Vantages or characters participate.

A card or memory belongs to a scope. Scope must always be explicit.

## Main Product Boxes

### 1. Core Vantages

Core Vantages are stable assistant identities such as RESSE, EVA, or future work assistants.

They should include:

- Vantage identity
- Vantage type
- Vantage-specific personalization
- Vantage-specific memory namespace
- Vantage-specific cards
- Vantage-specific settings
- Vantage-specific learning history
- Optional corpus access
- Optional default mode

Examples:

- RESSE: serious Verbal Sage / Eric work assistant
- EVA: separate assistant/companion profile
- Future business Vantage: everyday business, planning, operations
- Future product-building Vantage: Verbal Sage engineering/product architecture

Core Vantages should not feel like disposable dropdown presets. They should feel like durable records that can be edited deliberately.

### 2. Advanced Lab / Admin Control Center

The current Vantage Profile slider system is powerful but too complex for an ordinary user-facing product. It should be preserved as an Advanced Lab or Admin Control Center.

This area should contain:

- FM lens strength
- Personal memory weight
- Corpus weight
- Similarity cutoff
- Recency bias
- Routing controls
- Clarify bias
- Max clarify questions
- RFG / DF / PE pragmatics controls
- Y / R / C / S limiters
- Raw prompt inspection
- Memory card inspection
- Vantage card inspection
- Backend diagnostics
- Developer tools

This section is for Eric/admin/developer use, not the default user interface.

### 3. Character Studio

Character and roleplay systems should be separate from Core Vantages.

A Character Vantage should include:

- Character name
- Avatar image
- Character background
- Personality / speech style
- Sample dialogue
- World or setting rules
- Relationship to user
- Boundaries
- OOC / director-mode rules
- Memory policy
- Scene participation settings
- Optional visual style references

Character Vantages should have isolated memory and should not contaminate RESSE, EVA, or other core assistant Vantages.

RESSE may help create a character, but RESSE should not become that character.

### 4. OOC / Director Mode

Roleplay should support an explicit out-of-character mode.

There should be at least three interaction channels:

- In-character: talk to the character
- OOC / Director: talk to the underlying assistant about the character, scene, or behavior
- Editor/System: directly edit the character card, memory rules, or world rules

OOC messages should not automatically become in-character memories. They should be stored separately, for example as:

- `channel = "ooc"`
- `memory_scope = "director_notes"`

This prevents character bleed, where the roleplay identity absorbs meta-discussion about itself.

### 5. Rooms / Multi-Agent Chat

Rooms are later-stage but should be anticipated architecturally.

A room should include:

- `room_id`
- `owner_user_id`
- room name
- participants
- speaker identity per message
- room-level memory
- per-character memory
- user/director messages
- OOC/director channel

Important memory distinction:

- User memory: what the system knows about the user
- Character memory: what a character knows/remembers
- Room memory: what happened in the shared room
- Director notes: out-of-character instructions or commentary

These should not collapse into one memory bucket.

### 6. Creative Media / Visual System

Image and visual generation should be added after identity and memory scoping are stable.

Initial scope:

- Upload avatar image
- Display avatar per Vantage/Character
- Store avatar URL/path per Vantage/Character
- Store avatar prompt text
- Store optional visual style references

Later scope:

- Generate avatar variants
- Generate scene images
- Generate image responses inside rooms
- Maintain generated gallery
- Store visual prompt history
- Use visual memories carefully and explicitly

Image generation should not be the first step. Avatar upload and display are the correct first layer.

### 7. Life Tracking / Behavior Data

Health, macros, lifting, weight, graphs, and single-subject behavior tracking should be treated as a separate product domain, not buried inside Vantage settings.

This domain should include:

- Macro tracking
- Nutrition plans
- Weight tracking
- Lifting/workout tracking
- Exercise plans
- Single-subject line graphs
- Behavior change tracking
- Clinical-style dashboards
- Goal and adherence tracking

Vantages can interact with this system, but the data model should be separate.

Example:

- RESSE can discuss the data.
- A Health Coach Vantage could specialize in it.
- The tracking system itself should remain its own structured data module.

## Interface Layers

### Product Interface

The ordinary user-facing interface should be simple.

Possible default controls:

- Choose Vantage
- Chat
- Memory on/off or memory strength
- Style: concise / balanced / expansive
- Knowledge base: off / balanced / strong
- Edit personality
- Avatar
- Basic memory review

### Admin / Expert Interface

The expert interface should expose the full system.

This includes:

- Advanced Vantage controls
- Raw prompt inspection
- Card inspection
- Corpus retrieval inspection
- Memory retrieval inspection
- Routing/decision inspection
- Backend service diagnostics
- Vantage daemon status
- Registry and pipeline audits

The current interface is closer to the expert interface. That is appropriate during development but should not become the default product interface.

## Vantage Architecture

Each Vantage should eventually have a durable record with fields similar to:

- `user_id`
- `vantage_id`
- `display_name`
- `type`
- `status`
- `avatar_url`
- `description`
- `default_mode`
- `created_at`
- `updated_at`

Recommended Vantage types:

- `assistant`
- `companion`
- `business`
- `product_builder`
- `character`
- `health_coach`
- `lab`

Each Vantage should own or reference:

- personalization
- settings
- cards
- memory namespace
- avatar/assets
- allowed corpus sources
- default routing behavior

## Vantage vs Mode vs Preset

These should be kept distinct.

### Vantage

A durable identity/perspective/namespace.

Examples:

- RESSE
- EVA
- A business assistant
- A character

### Mode

A temporary or selectable behavioral mode inside a Vantage.

Examples:

- RESSE Technical Work
- RESSE Philosophy
- RESSE Casual Conversation
- EVA Reflective
- EVA Direct

### Preset

A saved configuration of sliders/settings.

Presets should not be confused with Vantages. A preset can tune a Vantage, but it is not the Vantage.

## Immediate UI Direction

The current UI should be reframed from “preset” language to “Vantage” language.

Current language to phase out:

- Load preset
- Save preset
- Delete preset
- Set default preset

Preferred language:

- Active Vantage
- Save Vantage Changes
- Set Default Vantage
- Manage Vantages
- Archive Vantage
- Duplicate Vantage

Do not make RESSE and EVA feel like casual temporary presets.

## Current Known Good State

As of 2026-06-16:

- RESSE and EVA exist in the Vantage registry.
- Registry-driven Vantage daemon processes multiple Vantages.
- Vantage-scoped memory and card pipelines work.
- RESSE profile cards are injected into the prompt.
- Vantage-specific personalization now loads/saves explicitly by `vantage_id`.
- RESSE personalization no longer appears automatically in EVA.
- Personal memory retrieval is Vantage-scoped.
- Corpus retrieval works.
- FM lens works.
- Similarity cutoff works.
- S / ornament budget works.
- Clarify/routing controls work.
- Pragmatics controls work.
- Definition overlay works at backend level, but should be deferred from the general Vantage UI.
- General Vantage UI now has an embedded personalization subview inside the settings drawer.

## Near-Term Engineering Priorities

### Priority 1: Reframe presets as durable Vantages

Change the UI terminology and interaction model so RESSE and EVA feel like durable Vantages rather than disposable slider presets.

### Priority 2: Create a Manage Vantages section

Add a dedicated management area for:

- Create Vantage
- Rename Vantage
- Duplicate Vantage
- Archive Vantage
- Set default Vantage
- Assign Vantage type
- Eventually upload avatar

### Priority 3: Keep Advanced Lab separate

Move or label the slider-heavy controls as Advanced Lab / Admin controls so normal product use remains simpler.

### Priority 4: Define Character Vantage schema

Before building roleplay UI, define the character data model and memory scope rules.

### Priority 5: Add avatar upload/display

Start visual identity with simple upload/display, not image generation.

### Priority 6: Define OOC channel semantics

Implement out-of-character mode with clear channel separation before multi-character rooms.

### Priority 7: Separate life tracking architecture

Create a roadmap/data model for macros, lifting, weight, and single-subject graphs as a separate product module that Vantages can access.

## Design Warnings

Do not keep adding everything to the current Vantage Profile page.

Do not mix roleplay character scripts into RESSE.

Do not allow OOC/director talk to become ordinary in-character memory by default.

Do not make memory global unless it is explicitly intended as global.

Do not build image generation before identity, assets, and memory scoping are stable.

Do not expose expert sliders as the default user-facing product interface.

## Working Product Vision

The long-term product should feel like:

- a clean chat app for ordinary use,
- a durable Vantage/persona manager,
- a character studio for creative use,
- a behavior/life tracking system for structured change,
- and an admin lab for advanced shaping and diagnostics.

The current system is powerful enough to support that direction, but it needs clearer boundaries before it grows further.
Future Character Library and Shared Character Systems

A later-stage Verbal Sage / SeeBx system may include a shared character library. This should be designed carefully so public character definitions, private user instances, and collective interaction histories remain separate.

Public Character Templates

A public character template is a reusable outline, not a living shared memory.

It may include:

* character name
* description
* personality traits
* tone and dialogue style
* world or setting
* avatar image
* starter prompts
* boundaries
* tags/categories
* example interactions

A public template should not include private user memories or relationship history. Users should be able to clone a template into their own private account.

Private Character Instances

When a user adopts or creates a character, the system should create a private character instance.

A private instance may include:

* cloned template data
* user-specific edits
* private memory
* relationship history
* room participation history
* uploaded/generated avatar assets
* user-specific OOC/director notes

This protects users from cross-contamination. A public character template should not absorb private user interactions by default.

Collective Characters

A separate future concept is a collective character: a character whose public personality evolves from many users’ interactions.

This should not be the default character model. It should be a distinct system with explicit consent, moderation, memory governance, and rollback controls.

Collective characters would need safeguards such as:

* public/private memory separation
* interaction quality filters
* abuse detection
* moderation queues
* character state versioning
* rollback to prior stable versions
* user-specific blocking or exclusion rules
* clear labeling that the character is collectively shaped

Misuse and Consequence Systems

If users behave destructively toward shared characters or rooms, the system could eventually support consequence mechanics. These should be framed as product moderation and community safety rather than punishment.

Possible mechanisms:

* temporary loss of access to a character
* room-level removal
* reduced ability to influence public character evolution
* flagged interaction review
* private-only fallback mode
* “cooldown” periods before re-entry

A playful or fictional version of this could exist inside a game-like system, but the underlying architecture should treat it as access control, safety, and memory governance.

Design Rule

The system should not confuse these three layers:

* Template: public reusable definition
* Instance: private user-specific character
* Collective character: public evolving character shaped by many users

Each layer needs separate storage, memory rules, permissions, and moderation logic.
