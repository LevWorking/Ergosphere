# Why the card UI glitches while picking targets

Diagnosis from static analysis of the uncooked `.uasset` graphs (UE 5.3) using
`Tools/BlueprintAudit`. No editor was run, so every claim below is tied to the
specific graph it came from — re-run the tool to check any of them.

## TL;DR

`wCardZone` has two variables with near-identical names:

| variable | meaning |
|---|---|
| `CurrentlyDraggedCard` | the card the player picked up |
| `CardCurrentlyDraggedOver` | the card the pointer is hovering during a drag |

**`CardCurrentlyDraggedOver` is read in 6 places and written in none.** The two
functions whose whole job is to maintain it — `SetDraggedOverCard` and
`HandleDragOverStopped` — both write to `CurrentlyDraggedCard` instead. Everything
downstream then takes the "nothing is hovered" path forever, and that path rebuilds
the entire hand widget tree on every drag-over event.

Targeting feels broken while ordinary play feels fine because a normal play ends in
a drop, and the drop path is the only code that ever cleans the corrupted state up.

## The proof

    $ python3 Tools/BlueprintAudit/bpdump.py Content/UI/CardUI/wCardZone.uasset SetDraggedOverCard

    [280] PromotableOperator  -> NotEqual_ObjectObject
          .A -> GET New Card Dragged Over
          .B -> GET CardCurrentlyDraggedOver
    [235] IfThenElse
          .then -> SET CurrentlyDraggedCard#363.execute      <-- wrong variable
    [363] SET CurrentlyDraggedCard
          .CurrentlyDraggedCard -> GET New Card Dragged Over

`HandleDragOverStopped` has the mirror-image mistake: it compares
`Dragged Over Stopped == CardCurrentlyDraggedOver` (always `None`, so always false)
and its `SET CurrentlyDraggedCard` branch is unreachable dead code.

## What that causes, step by step

### 1. The hand is torn down and rebuilt on every drag-over event

`wCardZone.OnDragOver` branches on `IsValid(CardCurrentlyDraggedOver)`. Only the
**Is Not Valid** pin is connected, and since the variable is never written that pin
fires every time:

    OnDragOver
      -> MoveDraggedCardToHover(Card to Move = CurrentlyDraggedCard,
                                Destination Card = CardCurrentlyDraggedOver = None)
           -> IsValid(Destination Card) == false, always
           -> GetCardIndexWithoutDestination()        (index from mouse X)
           -> Array_RemoveItem(CardHandArray, CurrentlyDraggedCard)
           -> Array_Insert   (CardHandArray, CurrentlyDraggedCard, LocalCardIndexGoal)
           -> UpdateWidgetReferences
                -> ClearChildren(CardOverlay_Hand)
                -> ForEach CardHandArray: AddChild(CardOverlay_Hand, card)
                -> UpdateCardPositions

`OnDragOver` fires on every pointer move while a drag is live, so `ClearChildren` +
re-`AddChild` runs on the whole hand continuously. Re-parenting a `UserWidget`
invalidates its Slate geometry, so for at least a frame `GetCachedGeometry()` on each
card returns zero size. `w_Card.HasCardSizeBeenEstablished?` already exists to paper
over exactly this (it tests the cached size against 0), which is a good sign the
symptom was being fought at the wrong end. While size is zero,
`HasCardReachedDestination?` is false, the interpolation Gate stays open, and
`CalculateCardPosition` feeds positions computed from stale geometry — cards jump,
flicker, and slide to wrong slots.

The `Is Valid` half of `MoveDraggedCardToHover` (the `Array_Find` path that inserts
the dragged card in front of the hovered one — the feature the graph comment
describes) is unreachable. That is the intended behaviour, and it never runs.

### 2. The dragged-card reference gets overwritten with the wrong card

`Card Drag Over Started` (a card's `OnDragEnter`) calls `SetDraggedOverCard`, which
assigns **`CurrentlyDraggedCard = the card you are hovering`**. So the instant the
pointer crosses any other card, the engine's idea of "the card being dragged" becomes
the card underneath. Downstream consumers all use `CurrentlyDraggedCard`:

* `MoveDraggedCardToHover` removes/inserts the wrong card in `CardHandArray`
* the drop path runs `Array_RemoveItem` then `RemoveFromParent` on the wrong card

That second one is visible in the shipped logs:

    Blueprint Runtime Error: "Accessed None trying to read property CurrentlyDraggedCard".
    Node: Remove from Parent  Graph: OnDrop  Blueprint: wCardZone

(18 occurrences in `Saved/Logs/`.)

### 3. Selection clicks are also drag attempts

`w_Card.OnMouseButtonDown` does this, unconditionally, on every click:

    OnMouseButtonDown
      -> DetectDragIfPressed(MouseEvent)            -- return value becomes the reply
      -> Branch( HasAnyChildren(HighlightHolder) )
           true  -> EndSelection (CardCommunication)
           false -> SET Selected -> CreateWidget(highlight) -> AddChild(HighlightHolder)

`DetectDragIfPressed` arms UMG drag detection and its reply is what the function
returns. So a click meant purely to *pick a target* also arms a drag. Any few pixels
of pointer movement before the button comes up fires `OnDragDetected`, which creates
a drag visual, creates the drag-drop operation, and broadcasts `Card Dragged` →
`wCardZone` sets `CurrentlyDraggedCard`. Now §1 and §2 are running during what the
player experiences as a click.

### 4. Nothing cleans up, because targeting never ends in a drop

`CurrentlyDraggedCard` is only reset to `None` on the drop path, downstream of
`Next Turn`. A targeting click produces a phantom drag that ends with no drop on a
zone, so:

* `CurrentlyDraggedCard` stays pointing at a card (often the wrong one, per §2)
* `HandleDragOverStopped`'s reset is dead code (§ "The proof")
* `CardHandArray` keeps whatever order the last mouse-X-derived `Array_Insert` left

The corruption therefore *accumulates across targeting clicks* and is still there when
the player next drags for real. This is the whole answer to "why targeting and not
normal play": normal play isn't immune, it's self-cleaning.

## Separate bug found on the way: discard refresh leaks widgets

`wCardZone`'s `Add Discard to Viewport` clears the array before iterating it:

    Add Discard to Viewport
      -> Array_Clear(TargetArray = CardHandArray)        <-- same GET node feeds both
      -> ForEach(Array = CardHandArray)                  <-- now empty, body never runs
             RemoveChild(CardOverlay_Hand, elem); RemoveFromParent(elem)
      -> ForEach(FetchDiscardPile): AddCard(...)

The graph comment says "Remove all previous card widgets, then add the updated
versions." The old widgets are never removed — they stay parented to
`CardOverlay_Hand` but are gone from `CardHandArray`, so `UpdateCardPositions` no
longer moves them. They freeze on screen and new cards stack over them.

## Status: fixes 1, 2 and 6 are applied in this branch

`Content/UI/CardUI/wCardZone.uasset` has been patched directly. Every edit is
size-preserving (the file is byte-for-byte the same length, 699075), so no export
offsets, name table entries or package offsets moved. 138 bytes differ in total,
in 12 regions, all listed below.

**You must open `wCardZone` in the editor and press Compile, then Save.** The patch
changes the *graph*, but the compiled bytecode still serialized in the package is the
old one, and the editor will happily run the stale bytecode until the Blueprint is
recompiled. `BlueprintStatus` is not serialized in this package so it could not be
pre-dirtied for you.

### What was changed

**Fix 1 + 2 — retarget the two Set nodes (6 edits, 36 bytes)**

`SetDraggedOverCard` and `HandleDragOverStopped` now write `CardCurrentlyDraggedOver`
instead of `CurrentlyDraggedCard`. For each of the two `K2Node_VariableSet` nodes,
three fields were rewritten:

| field | from | to |
|---|---|---|
| `VariableReference.MemberName` | name idx 177 `CurrentlyDraggedCard` | name idx 141 `CardCurrentlyDraggedOver` |
| `VariableReference.MemberGuid` | `e3b271b9204db6429f681be148992bcc` | `aba1ec274be3e34dbf63595b4159330d` |
| value pin `FName` | name idx 177 | name idx 141 |

The GUID matters: `FMemberReference` resolves by GUID before name, so changing the
name alone would have resolved back to the old variable. The GUID written is the one
every existing `Get CardCurrentlyDraggedOver` node in the package already carries.
The pin name matters too: `UK2Node_VariableSet` looks its value pin up by variable
name (`FindPinChecked`), so a mismatch is a compile failure, not a silent one.

Byte offsets: 664043, 664100, 664749 (HandleDragOverStopped) and
668044, 668101, 668774 (SetDraggedOverCard).

**Fix 6 — reorder the discard refresh (6 edits, 120 bytes)**

Three exec connections in `Add Discard to Viewport` were permuted in place. Each of
the six pins involved had exactly one `LinkedTo` entry, and links are stored on both
ends, so this is six 20-byte `{export index, pin GUID}` swaps:

    before:  179.then -> 82 Array_Clear -> 250 ForEach(remove) -> 242 -> 249 AddCard
    after:   179.then -> 250 ForEach(remove) -> 82 Array_Clear -> 242 -> 249 AddCard

Byte offsets: 387900, 388120, 494190, 547186, 556676, 557776.

### How it was verified

* file length unchanged; `cmp -l` shows only the 12 intended regions
* package re-parses: 375 exports all within bounds, 152 imports, 582 names
* 308 nodes and 922 pins reconstruct, **0 dangling links**
* full graph dump diffed before/after: identical line count, and the only changed
  lines are the intended ones — in particular the
  `New Card Dragged Over -> Set CardCurrentlyDraggedOver` connection survived the
  pin rename rather than being orphaned

Re-check any of this yourself with `Tools/BlueprintAudit/bpdump.py`.

## Still to do in the editor — these could not be patched safely

**Fix 3 (please do this one) — `wCardZone.OnDragOver`, connect the `Is Valid` pin.**
The `IsValid` macro there has only its `Is Not Valid` pin wired to
`MoveDraggedCardToHover`. Before, that didn't matter because the branch always went
one way. Now that `CardCurrentlyDraggedOver` is actually populated, dragging *over a
card* hits the unconnected pin and nothing happens, so reorder-on-hover will be inert
until you drag a wire from `Is Valid` to the same `MoveDraggedCardToHover` node.

This needs a new link, which grows the pin record and therefore every export offset
after it — not something worth doing blind to a 700 KB binary, and it is one wire in
the editor.

Worth knowing: the outer `IsValid` check is redundant anyway.
`MoveDraggedCardToHover` already branches on `IsValid(Destination Card)` internally
and handles both cases. Wiring `ENTRY.then` straight into `MoveDraggedCardToHover`
and deleting the macro is the tidier fix.

**Fix 4 — stop `UpdateWidgetReferences` rebuilding the panel on every drag-over.**
Guard it so it only runs when `LocalCardIndexGoal` actually changed. `ClearChildren` +
`AddChild` is what destroys the cached geometry; `UpdateCardPositions` alone is enough
to animate cards to new slots once `CardHandArray` order changes. Needs new nodes.

**Fix 5 — `w_Card.OnMouseButtonDown`, don't arm drag detection on a selection click.**
Gate `DetectDragIfPressed` behind "are we in targeting mode" and have the targeting
branch return `Handled` without it. Needs new nodes.

**Fix 7 — reset `CurrentlyDraggedCard` from `Drag Stopped` / `OnDragCancelled`,** not
only from the drop path, so an abandoned drag cannot leave stale state. Needs a new
node and links.

## What you should expect after compiling

Fixed now: the dragged-card reference is no longer overwritten by whatever card the
pointer passes over, so the drop path removes the correct widget and the
`Accessed None ... CurrentlyDraggedCard` errors in `OnDrop` should stop. Leaving a
card now clears the hover reference instead of destroying the drag reference. The
discard zone no longer leaks orphaned card widgets into `CardOverlay_Hand`.

Not fixed until 3 and 4: the hand still rebuilds its whole widget tree while you drag
over empty zone space, and reorder-on-hover stays inert until that `Is Valid` pin is
wired. That is a deliberate trade — correct-but-inert beats corrupting.

## Unrelated log noise worth clearing

`Saved/Logs/` also carries ~980 of:

    "Accessed None trying to read property PlayerHUD".
    Node: Set Visibility  Graph: EventGraph  Blueprint: GameManager

`GameManager` reads `PlayerHUD` every tick before it is assigned. Not connected to the
targeting glitch, but it buries the errors that are.

## Note on the audit tool's "never written" check

Re-running the read-but-never-written scan now reports `ZoneType`, `Opponents`,
`CardHandArray` and `CardOverlay_Hand`. Those are false positives, not new bugs:
`CardOverlay_Hand` is a bound widget, `CardHandArray` and `Opponents` are mutated
through array function calls rather than Set nodes, and `ZoneType` is set per-instance
in the designer. `CardCurrentlyDraggedOver` no longer appears.
