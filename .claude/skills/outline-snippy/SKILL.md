---
name: outline-snippy
description: Draw named outlines around the pieces of a new collage on the artist's own Snippy page, so the artist can check them and then cut. Use when the artist says "outline my new collage" (or "outline the collage I just dropped"). Free: never starts a paid cut.
---

# Outline a new collage

Snippy cuts pieces only from a collage that already has outlines. When the artist drops a collage on their page it waits there with the words "Awaiting component outlines". This skill is how it gets them: you look at the picture, draw one outline around each piece worth keeping, and save them where the page reads them. The artist then checks the outlines on their page, fixes or rejects any they do not like, and only then presses Extract and Confirm extraction themselves.

Plain words with the artist, no jargon. Never say "polygon", "JSON" or "record" to them: say "outlines", "your page", "saved".

## Rules (always)
- You only draw and save outlines. This costs nothing. Never press Extract, Confirm extraction, Approve or Resume, never call `start_job`, never start a cut, never call any Snippy helper tool. Cutting costs the artist money and the buttons are theirs.
- Never touch the OpenAI key, the settings or anything outside the page's outline records.
- Never save outlines you have not looked at the picture for. If you cannot see the picture, say so and ask the artist to attach the same picture to the chat. Do not guess from the file name.
- Save outlines only for the collage the artist named. If several collages are waiting and they did not say which, list their names and ask.
- The page checks every outline and refuses a bad one out loud. If the artist tells you the page refused something, read the reason on the page, fix that outline and save again.

## Steps

1. **Open the page's saved data.** Use the ArtifactData tool (its get, list and batch actions read and write a published page's stored records) with the artist's page url. To find the url, use the Artifact tool's list action and pick the artist's own artifact titled "Snippy Pieces"; if there are two or none, or you are unsure, ask the artist which page is theirs and do not guess. Read the record `inbox/index` (collection `inbox`, document `index`). Its `sources` list holds every collage the artist dropped: `key`, `name`, `previewUrl`, `originalUrl`. If the page is not published yet, or you cannot open its data, tell the artist plainly and stop (suggest "publish my Snippy page" first).
2. **Find the collage.** Waiting ones are the sources with no document named `outline-` followed by the first 16 characters of their `key` in the `inbox` collection. If there is exactly one, use it. If there are several, ask which.
3. **Look at the picture.** Take the stored picture's asset id: the 32 characters (0-9, a-f) after `/_blob/` in the source's `originalUrl` (use `previewUrl` if there is no `originalUrl`). Call the Artifact tool's read action with `path` set to that asset id; it saves the picture as a local file, which you then open and look at. Only if that fails, ask the artist to attach the same picture to the chat as a last resort. Then decide what the pieces are. A piece is something the artist could reuse in another collage: a complete object, a character, a meaningful group that belongs together (a boat with its person, a sign with its pole), or a useful texture patch. Keep connected parts together; do not split every leaf from its plant. Skip background, empty margin and scraps that are not reusable. Fewer, clearly useful pieces beat many tiny ones. No more than 24. An empty result is fine: if nothing is worth cutting, tell the artist and save nothing.
4. **Draw one outline per piece**, following its real edge closely, a hair outside it so nothing is clipped. Use 8 to 30 points. Do not redraw or invent parts that are hidden behind something else: outline only what is visible.
5. **Check with the artist before saving.** List the pieces in words: how many, and a numbered name for each ("1 Red fox, 2 Wooden sign with pole, 3 Barrel"). Ask whether these are the right pieces. Wait for their yes or their changes ("skip 3", "also the crate"), apply the changes, then save. Do not save before they answer.
6. **Save one record** with ArtifactData: collection `inbox`, document `outline-<first 16 characters of the source's key>` (write over the same document to redo it). Its content, with every number a percentage of the picture (0 to 100, from the top-left corner: x across, y down):

```
{
  "kind": "outlines",
  "key": "<the full key from inbox/index>",
  "title": "<short name for the collage, 80 characters or less>",
  "note": "<optional one line for the artist, 300 characters or less>",
  "regions": [
    {
      "id": "1",
      "name": "Red fox",
      "box": [x, y, width, height],
      "polygon": [[x, y], [x, y], [x, y], ...]
    }
  ]
}
```
   - `id` is "1", "2", "3" and so on, as text, never repeated. The page colours and numbers each outline from it: the same distinct colours the page already draws.
   - `name`: the piece's name in plain words (1 to 80 characters). The artist sees it on the page and it names the finished file.
   - `box` is the smallest rectangle around the piece: left, top, width, height. It must stay inside the picture.
   - `polygon` is the outline: three or more `[x, y]` points in order around the edge. Every point must sit inside (or within 3 of) its box. An outline with almost no area is refused. For a piece with a hole or that is in separate parts, use `"contours": [[...loop 1...], [...loop 2...]]` instead of `polygon`.
   - Do not add `reconstruction_required`; rebuilding hidden parts is not offered here.
7. **Check what you saved** against the rules above (ids "1", "2"..., every number 0 to 100, at least 3 points, points inside the box). If any rule is broken, fix it and save again before you say anything.
8. **Tell the artist what to do next**, in these plain words: "I saved N outlines. Open your Snippy page: the collage now shows with numbered coloured outlines. Expect to redo some, because I place them by eye: look at each one and tell me which are off, and I will redraw them. You can also use the page's own buttons: Comment on an outline, the small thumbs-down picture next to an outline (it has no text label) to mark that outline not wanted, or Skip image for the whole collage. Nothing is cut yet and nothing has been charged. When you are happy, press Extract on the collage; the page then shows a price box, and Confirm extraction there is the step that costs money, and it starts the cutting straight away: there is no separate Send button. If the helper ever refuses or stops, the page shows an Approve or Resume button with the price; pressing it is your decision too. The price box also shows your Snippy spending limit and how much is spent. Those buttons are your decision, not mine."
9. **If the page refused something**, the artist has to tell you what it says: you cannot see the page's message. Ask them to read out the line under "Your collages". Fix the outlines it names, save again and tell the artist.

## Redoing outlines
When the artist asks for changes, write the record again under the same name with the new outlines. The page notices, shows the new outlines, and asks for a fresh check of that collage. Do this as often as they like; it is free.
