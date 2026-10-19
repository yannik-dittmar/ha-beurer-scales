# Teach a new scale user with the one step-on that wakes the scale

Creating a scale user needs the scale awake, which needs a step-on, and that step-on already yields a live reading once HA has consented the new scale user. Hardware checks (#17, protocol doc §9.6) showed that one reading teaches the scale: a scale user taught with a single reading was recognised (`U-2`) at the next weigh-in. So the create flow and the repair "create again" flow ask for exactly one step-on ("stay on until the display goes dark"), show progress (connecting, creating the scale user, measuring) and end with the measured weight. We dropped the earlier "reading k of 3" loop and its teach-more menu.

## Considered options

- **Up to three readings with a menu to finish early** (the earlier design): rejected. Extra readings add nothing the hardware could show, and the scale only takes a new weigh-in after it has shown all results and gone dark, so people who stepped on again too early produced nothing and got confused. The menu also depended on the flow framework's progress-to-menu hand-over, which was skipped once in a driven flow.
- **Create the scale user without any reading and leave teaching to the next weigh-in**: rejected. The scale user would be unrecognised until someone happened to step on, and HA couldn't confirm that the user works.

## Consequences

- A failure before the reading still undoes the creation: the scale user is deleted, or the deletion is queued if the link broke (#21), because an untaught scale user steals recognition from the others.
- Re-teaching later (a "Teach" button, design §9) would need its own flow.
