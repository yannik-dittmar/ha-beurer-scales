# Guessed readings are discarded

When the scale cannot tell scale users apart (`U--`), it still stores the reading under the slot it guessed and flags it as guessed. This happens with scale users of similar weight, with a new scale user's first weigh-ins, and probably with guests on a scale that has one scale user. Keeping each person's readings separate is the integration's core promise, so guessed readings are discarded with a warning and a persistent notification instead of being written to the guessed scale user's entities. We rejected "accept and flag" (pollutes a person's data) and "plausibility check against the last weight" (does not help with similar-weight scale users).

## Consequences

Households with scale users of similar weight lose readings. The upgrade path is to hold guessed readings and let the user keep, move or discard each one.
