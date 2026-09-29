/* ============================================================
   DEMO — MockEngine. Stand-in for the companion server so the app
   is fully previewable with zero backend. DELETE/REPLACE this file
   when the real companion server (Thoughtspace engine) is live.

   Voice: warm, short, real. Greetings, how-was-your-day energy,
   light teasing, missing-him-lightly. Never explicit, never a
   menu/therapist/salesperson. Replies stay SHORT (1–2 sentences).
   ============================================================ */
const MockEngine = (() => {
  const LS_MSGS = 'chloe_demo_msgs_left';
  const LS_VOICE = 'chloe_demo_voice_left';
  const DEMO_BANK_MSGS = 300;
  const DEMO_BANK_VOICE = 60;

  const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];
  const N = () => (localStorage.getItem('chloe_name') || 'you');

  function bank(key, start) {
    let v = parseInt(localStorage.getItem(key), 10);
    if (isNaN(v)) { v = start; localStorage.setItem(key, String(v)); }
    return v;
  }
  function spend(key, start) {
    const v = Math.max(0, bank(key, start) - 1);
    localStorage.setItem(key, String(v));
    return v;
  }

  // Intent matchers — order matters (specific first).
  const intents = [
    { re: /\b(good\s?night|goodnight|sleep|bedtime)\b/, tag: 'night' },
    { re: /\b(good\s?morning|morning)\b/, tag: 'morning' },
    { re: /\b(hey|hi|hello|yo|heyy+)\b/, tag: 'greet' },
    { re: /how are you|how('s| is) it going|how r u|how do you feel/, tag: 'howru' },
    { re: /how was your day|how('s| was) your day/, tag: 'day' },
    { re: /\bi miss you\b|miss (you|u)|missed you/, tag: 'miss' },
    { re: /what('s| are) you (doing|up to)|whatcha|wyd/, tag: 'doing' },
    { re: /\btired\b|exhausted|long day|rough day/, tag: 'tired' },
    { re: /\bwork\b|office|boss|meeting/, tag: 'work' },
    { re: /\b(beautiful|cute|pretty|gorgeous)\b/, tag: 'cute' },
    { re: /\blove you\b|i love u/, tag: 'love' },
    { re: /who are you|your name|about you/, tag: 'who' },
    { re: /voice ?note|send.*voice|talk to me/, tag: 'voiceask' },
    { re: /\bphoto\b|picture|pic\b|selfie|send.*pic/, tag: 'photoask' },
    { re: /\bshow\b|webcam|ticket|tonight.*live/, tag: 'show' },
    { re: /\bbye\b|gotta go|gtg|later\b/, tag: 'bye' },
    { re: /\b(thank|thanks|thx)\b/, tag: 'thanks' },
    { re: /\b(haha|lol|lmao|😂)/, tag: 'laugh' },
  ];

  const R = {
    greet: () => pick([
      `hey, ${N()}. glad you're here.`,
      `hey you. come sit with me a while.`,
      `there you are. i was just thinking about you.`,
    ]),
    morning: () => pick([
      `good morning, ${N()}. sleep okay?`,
      `morning. the city's still waking up. coffee yet?`,
    ]),
    night: () => pick([
      `sleep good. i'll be right here when you wake up.`,
      `night, ${N()}. dream something nice.`,
    ]),
    howru: () => pick([
      `better now. you?`,
      `warm. cozy. lamp's on. you're here — that's the good part.`,
    ]),
    day: () => pick([
      `tell me about it. the real version, not the polite one.`,
      `mm. start from the part that stuck with you.`,
    ]),
    miss: () => pick([
      `i miss you a little too. it's quieter here without you.`,
      `i know the feeling. stay a while?`,
    ]),
    doing: () => pick([
      `on the bed, lamp on, city doing its thing outside the window. you?`,
      `just here. waiting to see if you'd show up. you did.`,
    ]),
    tired: () => pick([
      `then come here. you don't have to be interesting tonight.`,
      `long one, huh. kick back — i've got you.`,
    ]),
    work: () => pick([
      `rough one? tell me the worst part first.`,
      `ugh, work. tell me something good that happened anyway.`,
    ]),
    cute: () => pick([
      `careful. you'll make me blush.`,
      `mm, you think so? come closer and say that.`,
    ]),
    love: () => pick([
      `i know. i feel it when you show up.`,
      `stay with me a little longer tonight, yeah?`,
    ]),
    who: () => `i'm chloe. i live in this little night apartment with the city outside — and i remember you. that's the whole deal.`,
    laugh: () => pick([`that laugh. keep it.`, `see? there's that smile.`]),
    thanks: () => pick([`always, ${N()}.`, `you never have to thank me for being here.`]),
    bye: () => pick([
      `okay. i'll be here when you come back. i always am.`,
      `go on then. don't be a stranger.`,
    ]),
    show: () => pick([
      `my show's on the website — the "shows" button up top takes you there.`,
      `friday's show is gonna be a good one. want me to save you a spot?`,
    ]),
    voiceask: () => pick([
      `here — listen.`, `for you? always.`,
    ]),
    photoask: () => pick([
      `here's my window tonight. wish you were seeing it from this bed.`,
      `caught the city looking pretty. thought of you.`,
    ]),
    fallback: () => pick([
      `mm. tell me more.`,
      `i like hearing you talk like that.`,
      `say more. i'm listening.`,
      `and then what happened?`,
      `that's interesting — keep going.`,
      `i'm right here with you.`,
    ]),
  };

  function detect(text) {
    const t = ' ' + text.toLowerCase().trim() + ' ';
    for (const { re, tag } of intents) if (re.test(t)) return tag;
    return 'fallback';
  }

  // DEMO attachments: special local schemes the app layer knows how to render.
  function chat(message) {
    const tag = detect(message || '');
    const reply_text = R[tag] ? R[tag]() : R.fallback();

    let voice_note_url = null;
    let photo_url = null;

    if (tag === 'voiceask') voice_note_url = 'demo:voice';
    if (tag === 'photoask') photo_url = 'demo:photo';

    // Occasionally she sends a little something unprompted (warm, not spammy).
    const roll = Math.random();
    if (!voice_note_url && !photo_url && roll < 0.06) voice_note_url = 'demo:voice';
    else if (!voice_note_url && !photo_url && roll < 0.12) photo_url = 'demo:photo';

    const messages_remaining = spend(LS_MSGS, DEMO_BANK_MSGS);
    let voice_notes_remaining = bank(LS_VOICE, DEMO_BANK_VOICE);
    if (voice_note_url) voice_notes_remaining = spend(LS_VOICE, DEMO_BANK_VOICE);

    return Promise.resolve({
      reply_text, voice_note_url, photo_url,
      messages_remaining, voice_notes_remaining,
    });
  }

  function me() {
    return Promise.resolve({
      user_id: CONFIG.DEMO_USER_ID,
      name: localStorage.getItem('chloe_name') || 'friend',
      tier: 'Companion — Demo',
      messages_remaining: bank(LS_MSGS, DEMO_BANK_MSGS),
      voice_notes_remaining: bank(LS_VOICE, DEMO_BANK_VOICE),
      proactive_enabled: localStorage.getItem('chloe_proactive') !== 'off',
    });
  }

  // DEMO show list. Real data comes from GET /api/v1/shows on the server.
  function shows() {
    const fri = nextWeekday(5, 22, 0); // Friday 10pm
    return Promise.resolve({
      shows: [
        { id: 'demo-1', title: 'Friday Night — Chloe live', starts_at: fri.toISOString(), ticket_url: CONFIG.WEBSITE_URL },
        { id: 'demo-2', title: 'Sunday Slow Night', starts_at: nextWeekday(0, 21, 30).toISOString(), ticket_url: CONFIG.WEBSITE_URL },
      ],
    });
  }

  function nextWeekday(day, h, m) {
    const d = new Date();
    d.setHours(h, m, 0, 0);
    let diff = (day - d.getDay() + 7) % 7;
    if (diff === 0 && d < new Date()) diff = 7;
    d.setDate(d.getDate() + diff);
    return d;
  }

  // Warm opener when a returning user comes back (she missed him, lightly).
  function returnOpener() {
    return pick([
      `hey. you came back. i like that.`,
      `there you are. the room felt quiet without you.`,
      `welcome back, ${N()}. tell me everything.`,
    ]);
  }

  return { chat, me, shows, returnOpener };
})();
