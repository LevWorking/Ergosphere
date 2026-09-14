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

## Fixes, in priority order

1. **`wCardZone.SetDraggedOverCard`** — retarget the `SET` node to
   `CardCurrentlyDraggedOver`. (Right-click the Set node → change variable, or drop a
   fresh `Set CardCurrentlyDraggedOver` and rewire `New Card Dragged Over` into it.)
2. **`wCardZone.HandleDragOverStopped`** — same retarget, so leaving a card clears
   `CardCurrentlyDraggedOver` back to `None`.
3. **`wCardZone.OnDragOver`** — connect the `Is Valid` pin of the `IsValid` macro to
   `MoveDraggedCardToHover` as well. With 1 and 2 fixed this is the branch that
   actually reorders around the hovered card.
4. **`wCardZone.UpdateWidgetReferences`** — stop calling `ClearChildren` +
   `AddChild` on every drag-over. Either guard it so it only runs when
   `LocalCardIndexGoal` actually changed, or reorder in place. Rebuilding the panel is
   what destroys the geometry; `UpdateCardPositions` alone is enough to animate cards
   to new slots once `CardHandArray` order changes.
5. **`w_Card.OnMouseButtonDown`** — don't arm drag detection on a selection click.
   Gate `DetectDragIfPressed` behind "are we currently in targeting mode", and have
   the targeting branch return `Handled` without it. This is the fix that makes
   targeting stop behaving like a drag at all.
6. **`wCardZone` `Add Discard to Viewport`** — move `Array_Clear` to *after* the
   removal `ForEach`.
7. Reset `CurrentlyDraggedCard` to `None` from `Drag Stopped` / `OnDragCancelled`,
   not only from the drop path, so an abandoned drag can't leave stale state.

Fix 1 and 2 are one-node changes each and should be done first — several of the other
symptoms are downstream of them and may simply disappear.

## Unrelated log noise worth clearing

`Saved/Logs/` also carries ~980 of:

    "Accessed None trying to read property PlayerHUD".
    Node: Set Visibility  Graph: EventGraph  Blueprint: GameManager

`GameManager` reads `PlayerHUD` every tick before it is assigned. Not connected to the
targeting glitch, but it buries the errors that are.
