# Owner Directive (verbatim) - 2026-07-03

Preserved word for word at the owner's request. This is the intake source
for the specs derived from it (see companion spec documents dated
2026-07-03 in this folder). Dictated text; read for intent, not grammar.

Supporting evidence referenced in the directive: the machine-local crash
case at `D:\0x74_case_handoff\` (analysis machine). Key facts from that
case for repo readers without drive access: June 25-28 2026, five BSODs,
all 0x3B SYSTEM_SERVICE_EXCEPTION with exception 0xC0000094 (integer
divide by zero) in nvlddmkm.sys (NVIDIA driver build dated May 19 2026)
at the identical offset nvlddmkm+0x74342A, process context csrss.exe.
The case file concludes from the identical offset across all five dumps
that this is a deterministic driver bug rather than random corruption. The subsequent forced shutdowns produced a
dirty registry hive, a June 28 restore point captured it, and a July 1
System Restore installed it, causing a 0x74 BAD_SYSTEM_CONFIG_INFO boot
failure (repaired offline July 1). The owner suspects this application's
GPU workload is the trigger for the driver bug.

---

## Verbatim directive

> Please also ensure that when you read this, we need to ensure that the
> state docs within the WhisperSync are maintained or clearly
> understandable and very simple to navigate moving forward. While this
> repo has been updated various ways, I would like you to crawl and
> extensively understand this repo and present some of the changes that
> may improve it where you see current issues. Remember, this is a very
> low-level hardware-like application that interacts directly with the
> user's microphones, speakers, and therefore it is very prone to errors
> when we do not have pre-built handlers in place for those hardware
> devices. So, I mean, we do have drivers, but, you know, while this
> application isn't just a simple text editor or an HTML editor making
> API calls, it's a little more complex than that. Currently, I need to
> ensure that this app is modular in nature. It's easy to understand. It
> has had many changes over the time of its existence. So please validate
> that the kind of state machine is stable, it's modular. It's composable
> in a way that it's not relying on old-school timeout methods, or it is
> very... And ensure that you're also able to test all of this. That's
> the critical part here. Now, there should be a folder in here with
> testing procedures already built in. If there is not, then this repo
> needs to contain the previous testing procedures and the testing
> results. and what decisions were made on those testing results. You can
> probably check the prior commits on this repo for those specific items.
> We need to have docs also fully self-enclosed in this repos to ensure
> that any and all I point at this from within the repo or external to
> the repo can immediately understand it so that it's very clear there
> aren't straggling documents or anything like that. So that's your goals
> right now is A, clean up, B, project, C, understand the repo, and D,
> provide actionable decisions or choices which you can come back to me
> with. You can simply provide these in a list, a very succinct numbered
> list, single sentence title, single sentence definition. Imagine I'm a
> product manager. I don't need all of the crazy details, but I just need
> to know enough to understand the implications and so on. Now, there is
> another aspect to this that's very important, and that is the
> criticality of determining why this computer is crashing. I think it's
> this software. think we're running into a bug, whether it's in the
> NVIDIA driver or not, it doesn't matter. There was a divide by zero bug
> that was determined for this computer. And you can check on the D drive
> under the, there's a folder there which has the full analysis of this.
> And I think it's this software that's causing that to occur. Whether
> it's a driver error issue, it does not matter. Yes, that shouldn't
> happen, but we need to protect Fort here then if NVIDIA can't do that
> themselves. because this may happen on another GPU and these solutions
> need to be modular and native to being turned on or off, kind of like a
> feature. So that if we change drivers or devices, say for Intel, then
> we're not muddling up the whole software with one or two lines of code
> in one file. That will break. So that is your last task, is to
> investigate how this might be affecting the computer and how we can
> protect from that. I still think there are memory leaks. I still think
> there are multiple instances running at times because I can feel my
> computer drag aggressively. So I think we need some form of auto
> detection in this application. Let's say if we're running low on VRAM,
> like some kind of... Let's say we've hit... 500 megs or a gigabyte of
> VRAM left on the machine. I still think a gigabyte is fine, but like
> let's say 750 megs. When this reaches, the model automatically
> downloads and there's some kind of built-in handoff to the next model
> to continue. Now for recording meetings, I don't see this as an issue
> as the as it should technically be recording offline and then it gets
> handed to the model. When I say model, I mean Whisper. So I don't think
> it's a problem there. For dictation, it might be an issue. But even
> then, I think we have it built in place where technically it's being
> recorded offline so that we can record those and recover them. And then
> it's being sent to the model. I'm not sure. But regardless, I think
> both of these should probably be handled in a similar manner in this
> that's bad. But I think some form of auto helping with the model, and
> then we can detect when the model downgrades and say, and then we can
> honestly look at those times in Windows and say, oh, this might be
> going on. This is very useful for another application that I'm
> building, which extends this application and makes it API first. What I
> want you to do is to preserve my comments here. You can do this word
> for word, but this needs to be preserved somewhere, preferably in the
> other repo, as that's where this should be working. I do not want this
> in the PM repo at all. Then we need to create specs from this. If you
> want to use the skill for brainstorming superpowers, we can. That way
> it's spec-driven. Feel free. Or if you feel that you can do this as
> long as you have surfaces, you're recording progress. So if something
> stops midway, that's fine with me too.

---

## Derived work items (tracked in companion specs)

1. Docs navigation and self-enclosure cleanup (state docs simple to
   navigate; no straggling documents; repo fully self-explanatory).
2. Repo-wide architecture validation: state machine stability,
   modularity, composability, elimination of timeout-based coordination.
3. Testing procedures and results: a durable in-repo record of manual
   test procedures, past results, and the decisions made on them.
4. GPU guard: modular, feature-toggled protection layer (VRAM watermark
   monitoring, automatic model downgrade with handoff, downgrade event
   logging for OS-level crash correlation, multi-instance detection),
   vendor-agnostic so a device/driver change does not require edits
   scattered through the codebase.
