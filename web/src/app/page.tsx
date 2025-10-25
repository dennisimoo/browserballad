'use client';

import {
  type FormEvent,
  type ReactElement,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Loader2, Maximize2, MonitorPlay, PlayCircle, RefreshCw, Timer, Trophy, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

type StreamEvent = {
  type: string;
  status?: string;
  message?: string;
  result?: string;
  url?: string;
};

type RaceTask = {
  title: string;
  summary: string;
  human_instructions: string;
  agent_instructions: string;
  task_type: "text_entry" | "confirmation" | string;
  success_criteria: string;
  expected_output_description: string;
  evaluation_guidelines: string[];
};

type ParticipantState = {
  status: string;
  started_at: string | null;
  completed_at: string | null;
  duration_seconds: number | null;
  live_url?: string | null;
  result?: string | null;
};

type Verdict = {
  winner: "agent" | "human" | "tie" | string;
  reasoning: string;
  agent_score: number;
  human_score: number;
};

type RaceData = {
  race_id: string;
  status: string;
  task: RaceTask;
  agent: ParticipantState & { live_url: string | null; result: string | null };
  human: ParticipantState & { result?: string | null };
  verdict: Verdict | null;
};

type RaceResponse = { race: RaceData };
type AgentStartResponse = RaceResponse & { run_id: string };

const LIVE_URL_REGEX = /https:\/\/live\.browser-use\.com[^\s\u001b]*/i;
const STREAM_EVENT_TYPES = ["status", "log", "error", "result", "live_url", "complete", "message"] as const;

function normalizeApiOrigin(value: string | undefined): string {
  if (!value || value.trim().length === 0) {
    return "http://localhost:8000";
  }
  const trimmed = value.trim().replace(/\/$/, "");
  if (/^https?:\/\//i.test(trimmed)) {
    return trimmed;
  }
  if (trimmed.startsWith(":")) {
    return `http://localhost${trimmed}`;
  }
  return `http://${trimmed}`;
}

const API_ORIGIN = normalizeApiOrigin(process.env.NEXT_PUBLIC_AGENT_API);

const sanitizeLiveUrl = (url: string): string =>
  url.replace(/\u001b\[[0-9;]*m/g, "").replace(/\s+$/, "");

function buildEventHandler(onEvent: (event: StreamEvent) => void) {
  return (event: MessageEvent<string>) => {
    try {
      const parsed = JSON.parse(event.data) as StreamEvent;
      onEvent(parsed);
    } catch (error) {
      console.error("Unable to parse event", error);
    }
  };
}

function formatTimestamp(value: string | null): string {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleTimeString();
}

function formatDuration(value: number | null): string {
  if (value === null || Number.isNaN(value)) {
    return "—";
  }
  if (value < 1) {
    return `${(value * 1000).toFixed(0)}ms`;
  }
  return `${value.toFixed(1)}s`;
}

export default function HomePage(): ReactElement {
  const [race, setRace] = useState<RaceData | null>(null);
  const [agentRunId, setAgentRunId] = useState<string | null>(null);
  const [raceError, setRaceError] = useState<string | null>(null);
  const [humanSubmission, setHumanSubmission] = useState("");
  const [isCreatingRace, setIsCreatingRace] = useState(false);
  const [isStartingRace, setIsStartingRace] = useState(false);
  const [isSubmittingHuman, setIsSubmittingHuman] = useState(false);
  const [isRefreshingRace, setIsRefreshingRace] = useState(false);
  const [promptVisible, setPromptVisible] = useState(false);
  const [isLiveFullscreen, setIsLiveFullscreen] = useState(false);
  const liveFullscreenRef = useRef<HTMLDivElement | null>(null);
  const humanSectionRef = useRef<HTMLDivElement | null>(null);
  const hasScrolledToPromptRef = useRef(false);

  const eventSourceRef = useRef<EventSource | null>(null);

  const stopStreaming = useCallback(() => {
    eventSourceRef.current?.close();
    eventSourceRef.current = null;
  }, []);

  useEffect(() => {
    return () => {
      stopStreaming();
    };
  }, [stopStreaming]);

  const refreshRace = useCallback(
    async (raceId: string) => {
      setIsRefreshingRace(true);
      try {
        const response = await fetch(`${API_ORIGIN}/race/${raceId}`);
        if (!response.ok) {
          const detail = await response.text();
          throw new Error(`Failed to refresh race (${response.status}): ${detail}`);
        }
        const payload: RaceResponse = await response.json();
        setRace(payload.race);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setRaceError(prev => prev ?? message);
      } finally {
        setIsRefreshingRace(false);
      }
    },
    [],
  );

  const startStream = useCallback(
    (runId: string, raceId: string) => {
      stopStreaming();
      const eventSource = new EventSource(`${API_ORIGIN}/run/${runId}/events`);
      eventSourceRef.current = eventSource;

      const handleStreamEvent = buildEventHandler(eventData => {
        switch (eventData.type) {
          case "status":
            if (eventData.status) {
              setRace(prev => {
                if (!prev) {
                  return prev;
                }
                const incomingStatus = eventData.status ?? prev.agent.status;
                return {
                  ...prev,
                  status:
                    incomingStatus === "running" && prev.status === "ready"
                      ? "running"
                      : prev.status,
                  agent: {
                    ...prev.agent,
                    status: incomingStatus,
                    started_at:
                      prev.agent.started_at ?? (incomingStatus === "running" ? new Date().toISOString() : prev.agent.started_at),
                  },
                };
              });
            }
            break;
          case "log":
            if (eventData.message) {
              const potentialUrl = eventData.message.match(LIVE_URL_REGEX)?.[0];
              if (potentialUrl) {
                const cleanedUrl = sanitizeLiveUrl(potentialUrl);
                setRace(prev => {
                  if (!prev || prev.agent.live_url) {
                    return prev;
                  }
                  return {
                    ...prev,
                    agent: {
                      ...prev.agent,
                      live_url: cleanedUrl,
                    },
                  };
                });
              }
            }
            break;
          case "live_url":
            if (eventData.url) {
              const url = sanitizeLiveUrl(eventData.url);
              setRace(prev => {
                if (!prev) {
                  return prev;
                }
                return {
                  ...prev,
                  agent: {
                    ...prev.agent,
                    live_url: url,
                  },
                };
              });
            }
            break;
          case "result":
            if (eventData.result) {
              setRace(prev => {
                if (!prev) {
                  return prev;
                }
                return {
                  ...prev,
                  agent: {
                    ...prev.agent,
                    result: eventData.result ?? prev.agent.result ?? null,
                  },
                };
              });
            }
            break;
          case "error":
            if (eventData.message) {
              setRaceError(eventData.message);
            }
            setRace(prev => {
              if (!prev) {
                return prev;
              }
              return {
                ...prev,
                agent: {
                  ...prev.agent,
                  status: "error",
                },
              };
            });
            break;
          case "complete":
            eventSource.close();
            eventSourceRef.current = null;
            setRace(prev => {
              if (!prev) {
                return prev;
              }
              return {
                ...prev,
                agent: {
                  ...prev.agent,
                  status: prev.agent.status === "error" ? prev.agent.status : "completed",
                  completed_at: prev.agent.completed_at ?? new Date().toISOString(),
                },
              };
            });
            void refreshRace(raceId);
            break;
        }
      });

      STREAM_EVENT_TYPES.forEach(type => {
        eventSource.addEventListener(type, handleStreamEvent as EventListener);
      });

      eventSource.onerror = () => {
        eventSource.close();
        eventSourceRef.current = null;
        setRaceError(prev => prev ?? "Event stream interrupted. Check the API server logs.");
        void refreshRace(raceId);
      };
    },
    [refreshRace, stopStreaming],
  );

  const createRace = useCallback(async () => {
    setRaceError(null);
    stopStreaming();
    setIsCreatingRace(true);
    setPromptVisible(false);
    hasScrolledToPromptRef.current = false;
    try {
      const response = await fetch(`${API_ORIGIN}/race`, { method: "POST" });
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Failed to create race (${response.status}): ${detail}`);
      }
      const payload: RaceResponse = await response.json();
      setRace(payload.race);
      setAgentRunId(null);
      setHumanSubmission("");
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setRaceError(message);
    } finally {
      setIsCreatingRace(false);
    }
  }, [stopStreaming]);

  const handleStartRace = useCallback(async () => {
    if (!race) {
      return;
    }
    const raceId = race.race_id;
    setRaceError(null);
    setIsStartingRace(true);
    try {
      const humanResponse = await fetch(`${API_ORIGIN}/race/${raceId}/human/start`, {
        method: "POST",
      });
      if (!humanResponse.ok) {
        const detail = await humanResponse.text();
        throw new Error(`Failed to start race (${humanResponse.status}): ${detail}`);
      }
      const humanPayload: RaceResponse = await humanResponse.json();
      setRace(humanPayload.race);
      setPromptVisible(true);

      const agentResponse = await fetch(`${API_ORIGIN}/race/${raceId}/agent/start`, {
        method: "POST",
      });
      if (!agentResponse.ok) {
        const detail = await agentResponse.text();
        throw new Error(`Failed to start agent (${agentResponse.status}): ${detail}`);
      }
      const agentPayload: AgentStartResponse = await agentResponse.json();
      setRace(agentPayload.race);
      setAgentRunId(agentPayload.run_id);
      startStream(agentPayload.run_id, agentPayload.race.race_id);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setRaceError(message);
    } finally {
      setIsStartingRace(false);
    }
  }, [race, startStream]);

  const submitHuman = useCallback(
    async (submission: string | null) => {
      if (!race || !promptVisible) {
        return;
      }
      setRaceError(null);
      setIsSubmittingHuman(true);
      try {
        const response = await fetch(`${API_ORIGIN}/race/${race.race_id}/human/submit`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ submission }),
        });
        if (!response.ok) {
          const detail = await response.text();
          throw new Error(`Failed to record human submission (${response.status}): ${detail}`);
        }
        const payload: RaceResponse = await response.json();
        setRace(payload.race);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setRaceError(message);
      } finally {
        setIsSubmittingHuman(false);
      }
    },
    [promptVisible, race],
  );

  const handleHumanSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (!promptVisible) {
        return;
      }
      void submitHuman(humanSubmission.trim() || null);
    },
    [humanSubmission, promptVisible, submitHuman],
  );

  const handleOpenLiveFullscreen = useCallback(() => {
    setIsLiveFullscreen(true);
  }, []);

  const handleCloseLiveFullscreen = useCallback(() => {
    const fullscreenElement = document.fullscreenElement;
    if (fullscreenElement && fullscreenElement === liveFullscreenRef.current) {
      void document.exitFullscreen().catch(() => undefined);
    }
    setIsLiveFullscreen(false);
  }, []);

  useEffect(() => {
    if (!race || race.status !== "judging") {
      return;
    }
    const interval = window.setInterval(() => {
      void refreshRace(race.race_id);
    }, 3000);
    return () => window.clearInterval(interval);
  }, [race, refreshRace]);

  useEffect(() => {
    if (!race) {
      return;
    }
    if (race.status !== "ready" && !promptVisible) {
      setPromptVisible(true);
    }
  }, [promptVisible, race]);

  useEffect(() => {
    if (!promptVisible || hasScrolledToPromptRef.current) {
      return;
    }
    const section = humanSectionRef.current;
    if (section) {
      section.scrollIntoView({ behavior: "smooth", block: "start" });
      hasScrolledToPromptRef.current = true;
    }
  }, [promptVisible]);

  useEffect(() => {
    if (isLiveFullscreen) {
      document.body.classList.add("overflow-hidden");
    } else {
      document.body.classList.remove("overflow-hidden");
    }
    return () => {
      document.body.classList.remove("overflow-hidden");
    };
  }, [isLiveFullscreen]);

  useEffect(() => {
    if (!isLiveFullscreen) {
      return;
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setIsLiveFullscreen(false);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    const container = liveFullscreenRef.current;
    if (container && container.requestFullscreen) {
      void container.requestFullscreen().catch(() => undefined);
    }
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      if (document.fullscreenElement && document.fullscreenElement === container) {
        void document.exitFullscreen().catch(() => undefined);
      }
    };
  }, [isLiveFullscreen]);

  useEffect(() => {
    const handleFullscreenChange = () => {
      if (!document.fullscreenElement) {
        setIsLiveFullscreen(false);
      }
    };
    document.addEventListener("fullscreenchange", handleFullscreenChange);
    return () => {
      document.removeEventListener("fullscreenchange", handleFullscreenChange);
    };
  }, []);

  const agentLiveUrl = race?.agent.live_url ?? null;
  const agentStatus = race?.agent.status ?? "pending";
  const humanStatus = race?.human.status ?? "pending";
  const verdict = race?.verdict ?? null;
  const taskType = race?.task.task_type ?? "text_entry";
  const isJudging = race?.status === "judging";

  const disableHumanForm = !promptVisible || humanStatus === "completed" || humanStatus === "error";
  const raceStatus = race?.status ?? "awaiting_task";

  const runIdDisplay = useMemo(() => agentRunId ?? "—", [agentRunId]);
  const agentResultText = race?.agent.result ?? "";
  const humanResultText = race?.human.result ?? "";
  const humanResultPreview = humanResultText
    ? `${humanResultText.slice(0, 40)}${humanResultText.length > 40 ? "…" : ""}`
    : "—";

  return (
    <main className="min-h-screen bg-stone-950 text-stone-100">
      <div className="mx-auto w-full max-w-5xl space-y-10 px-4 py-10">
        <header className="space-y-3 text-center">
          <h1 className="text-3xl font-semibold tracking-tight text-stone-50">Ballad of Browsers</h1>
          <p className="text-stone-400">
            Spin up a fresh browser quest, and race the autonomous agent.
          </p>
          {/* <p className="text-sm text-stone-500">
            Status: {raceStatus}
            {isRefreshingRace ? " • syncing" : ""}
          </p> */}
          {raceError && (
            <p className="mx-auto max-w-xl rounded-xl border border-red-500/40 bg-red-500/10 px-4 py-2 text-sm text-red-200">
              {raceError}
            </p>
          )}
        </header>

        <section className="rounded-2xl border border-stone-800 bg-stone-900/60 p-6 shadow-lg shadow-stone-950/40">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="space-y-1">
              <p className="text-xs uppercase tracking-[0.28em] text-stone-500">Current Task</p>
              <h2 className="text-2xl font-semibold text-stone-50">
                {promptVisible
                  ? race?.task.title ?? "Race task"
                  : race
                    ? "Task hidden until start"
                    : "No task yet"}
              </h2>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button
                onClick={createRace}
                disabled={isCreatingRace || isStartingRace}
                className="bg-stone-800 text-stone-100 hover:bg-stone-700"
              >
                <RefreshCw className="mr-2 h-4 w-4" />
                {isCreatingRace ? "Generating" : "Generate Task"}
              </Button>
              {race && !promptVisible && (
                <Button
                  onClick={handleStartRace}
                  disabled={isStartingRace}
                  className="bg-stone-100 text-stone-900 hover:bg-stone-200"
                >
                  <PlayCircle className="mr-2 h-4 w-4" />
                  {isStartingRace ? "Starting" : "Start Race"}
                </Button>
              )}
            </div>
          </div>

          {!race ? (
            <p className="mt-6 text-sm text-stone-400">
              Tap <span className="font-medium text-stone-200">Generate Task</span> to create a new race, then
              press start when you are ready.
            </p>
          ) : !promptVisible ? (
            <div className="mt-6 rounded-xl border border-stone-800 bg-stone-950/40 p-4 text-sm text-stone-300">
              Task prepared. Launch the race to reveal the instructions and kick off both participants.
            </div>
          ) : (
            <div className="mt-6 rounded-xl border border-stone-800 bg-stone-950/40 p-4 text-sm text-stone-300">
              Prompt unlocked. Scroll down to the human submission panel to view the instructions.
            </div>
          )}
        </section>

        <section className="rounded-2xl border border-stone-800 bg-stone-900/60 p-6 shadow-lg shadow-stone-950/40">
          <h2 className="text-lg font-medium text-stone-100">Status Board</h2>
          <div className="mt-4 grid gap-4 md:grid-cols-2">
            <div className="rounded-xl border border-stone-800 bg-stone-950/40 p-4 shadow-inner shadow-stone-950/40">
              <div className="flex items-center justify-between text-sm">
                <span className="flex items-center gap-2 text-stone-400">
                  <MonitorPlay className="h-4 w-4" /> Agent
                </span>
                <span className="text-stone-200">{agentStatus}</span>
              </div>
              <dl className="mt-3 space-y-2 text-sm text-stone-300">
                <div className="flex justify-between">
                  <dt className="text-stone-500">Run ID</dt>
                  <dd className="text-stone-200">{runIdDisplay}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-stone-500">Started</dt>
                  <dd className="text-stone-200">{formatTimestamp(race?.agent.started_at ?? null)}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-stone-500">Completed</dt>
                  <dd className="text-stone-200">{formatTimestamp(race?.agent.completed_at ?? null)}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-stone-500">Duration</dt>
                  <dd className="text-stone-200">{formatDuration(race?.agent.duration_seconds ?? null)}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-stone-500">Live Feed</dt>
                  <dd className="text-stone-200">{agentLiveUrl ? "Ready" : "Pending"}</dd>
                </div>
              </dl>
              {agentResultText && (
                <div className="mt-4">
                  <Label className="text-xs uppercase tracking-[0.28em] text-stone-500">Agent Output</Label>
                  <pre className="mt-2 max-h-48 overflow-auto rounded-xl border border-stone-800 bg-stone-950/60 p-3 text-xs text-stone-200">
                    {agentResultText}
                  </pre>
                </div>
              )}
            </div>

            <div className="rounded-xl border border-stone-800 bg-stone-950/40 p-4 shadow-inner shadow-stone-950/40">
              <div className="flex items-center justify-between text-sm">
                <span className="flex items-center gap-2 text-stone-400">
                  <Timer className="h-4 w-4" /> Human
                </span>
                <span className="text-stone-200">{humanStatus}</span>
              </div>
              <dl className="mt-3 space-y-2 text-sm text-stone-300">
                <div className="flex justify-between">
                  <dt className="text-stone-500">Started</dt>
                  <dd className="text-stone-200">{formatTimestamp(race?.human.started_at ?? null)}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-stone-500">Completed</dt>
                  <dd className="text-stone-200">{formatTimestamp(race?.human.completed_at ?? null)}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-stone-500">Duration</dt>
                  <dd className="text-stone-200">{formatDuration(race?.human.duration_seconds ?? null)}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-stone-500">Submission</dt>
                  <dd className="text-stone-200">{humanResultPreview}</dd>
                </div>
              </dl>
            </div>
          </div>
        </section>

        {agentLiveUrl && (
          <section className="rounded-2xl border border-stone-800 bg-stone-900/60 p-6 shadow-lg shadow-stone-950/40">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <span className="flex items-center gap-2 text-sm text-stone-200">
                <MonitorPlay className="h-4 w-4 text-stone-400" /> Live session ready
              </span>
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  onClick={handleOpenLiveFullscreen}
                  className="bg-stone-100 text-stone-900 hover:bg-stone-200"
                >
                  <Maximize2 className="mr-2 h-4 w-4" /> Fullscreen
                </Button>
                <Button
                  asChild
                  className="bg-stone-800 text-stone-100 hover:bg-stone-700"
                >
                  <a href={agentLiveUrl} target="_blank" rel="noreferrer">
                    Open in new tab
                  </a>
                </Button>
              </div>
            </div>
            <div
              className={`mt-4 overflow-hidden rounded-2xl border border-stone-800 bg-stone-950 ${isLiveFullscreen ? "hidden" : ""}`}
            >
              <iframe
                key={agentLiveUrl}
                src={agentLiveUrl}
                title="Browser live session"
                className="h-[420px] w-full"
                allow="clipboard-read; clipboard-write; accelerometer; autoplay; camera; microphone"
              />
            </div>
          </section>
        )}

        <section
          className="rounded-2xl border border-stone-800 bg-stone-900/60 p-6 shadow-lg shadow-stone-950/40"
          ref={humanSectionRef}
        >
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-medium text-stone-100">Human Submission</h2>
            <Timer className="h-4 w-4 text-stone-500" />
          </div>

          {!promptVisible && (
            <p className="mt-3 rounded-xl border border-stone-800 bg-stone-950/40 p-3 text-sm text-stone-400">
              Start the race to reveal the prompt and unlock submissions.
            </p>
          )}

          <div className="mt-4 space-y-4">
            {promptVisible && (
              <div className="space-y-2">
                <p className="text-xs uppercase tracking-[0.28em] text-stone-500">Prompt</p>
                <p className="rounded-xl border border-stone-800 bg-stone-950/40 p-4 text-base text-stone-100 whitespace-pre-wrap">
                  {race?.task.human_instructions}
                </p>
              </div>
            )}
            {taskType === "text_entry"
              ? humanStatus !== "completed" && (
                  <form className="space-y-3" onSubmit={handleHumanSubmit}>
                    <div className="space-y-2">
                      <Label htmlFor="human-submission" className="text-xs uppercase tracking-[0.28em] text-stone-500">
                        Your Output
                      </Label>
                      <Textarea
                        id="human-submission"
                        value={humanSubmission}
                        onChange={event => setHumanSubmission(event.target.value)}
                        placeholder="Describe what you found."
                        rows={5}
                        disabled={disableHumanForm || isSubmittingHuman}
                        className="border-stone-800 bg-stone-950 text-stone-100 placeholder:text-stone-500 focus-visible:ring-stone-400/60 focus-visible:ring-offset-stone-950"
                      />
                    </div>
                    <Button
                      type="submit"
                      disabled={disableHumanForm || isSubmittingHuman}
                      className="bg-stone-100 text-stone-900 hover:bg-stone-200"
                    >
                      {isSubmittingHuman ? "Submitting" : "Submit Result"}
                    </Button>
                  </form>
                )
              : humanStatus !== "completed" && (
                  <Button
                    onClick={() => {
                      void submitHuman(null);
                    }}
                    disabled={disableHumanForm || isSubmittingHuman}
                    className="bg-stone-100 text-stone-900 hover:bg-stone-200"
                  >
                    {isSubmittingHuman ? "Submitting" : "Mark Completed"}
                  </Button>
                )}

            {humanResultText && (
              <div>
                <Label className="text-xs uppercase tracking-[0.28em] text-stone-500">Recorded Output</Label>
                <pre className="mt-2 rounded-xl border border-stone-800 bg-stone-950/60 p-3 text-xs text-stone-200 whitespace-pre-wrap">
                  {humanResultText}
                </pre>
              </div>
            )}
          </div>
        </section>

        {(isJudging || verdict) && (
          <section className="rounded-2xl border border-stone-800 bg-stone-900/60 p-6 shadow-lg shadow-stone-950/40">
            <div className="flex items-center gap-2 text-stone-200">
              <Trophy className="h-5 w-5 text-amber-300" />
              <h2 className="text-lg font-medium">Final Verdict</h2>
            </div>
            {isJudging && !verdict ? (
              <div className="mt-6 flex items-center justify-center gap-3 rounded-xl border border-stone-800 bg-stone-950/40 p-6 text-sm text-stone-300">
                <Loader2 className="h-5 w-5 animate-spin text-stone-400" />
                <span>Judging in progress…</span>
              </div>
            ) : (
              verdict && (
                <div className="mt-4 space-y-3">
                  <p className="text-sm text-stone-300">
                    Winner: <span className="font-semibold uppercase text-stone-50">{verdict.winner}</span>
                  </p>
                  <p className="text-sm text-stone-300">{verdict.reasoning}</p>
                  <div className="grid gap-3 md:grid-cols-2">
                    <div className="rounded-xl border border-stone-800 bg-stone-950/40 p-4 text-stone-200">
                      <p className="text-xs uppercase tracking-[0.28em] text-stone-500">Agent Score</p>
                      <p className="mt-2 text-3xl font-semibold text-stone-50">{verdict.agent_score.toFixed(1)}</p>
                    </div>
                    <div className="rounded-xl border border-stone-800 bg-stone-950/40 p-4 text-stone-200">
                      <p className="text-xs uppercase tracking-[0.28em] text-stone-500">Human Score</p>
                      <p className="mt-2 text-3xl font-semibold text-stone-50">{verdict.human_score.toFixed(1)}</p>
                    </div>
                  </div>
                </div>
              )
            )}
          </section>
        )}

        {isLiveFullscreen && agentLiveUrl && (
          <div ref={liveFullscreenRef} className="fixed inset-0 z-50 flex flex-col bg-stone-950/95">
            <div className="flex items-center justify-between border-b border-stone-800 bg-stone-950/90 px-4 py-3">
              <span className="flex items-center gap-2 text-sm text-stone-200">
                <MonitorPlay className="h-4 w-4 text-stone-400" /> Live session
              </span>
              <div className="flex items-center gap-2">
                <Button
                  asChild
                  className="bg-stone-800 text-stone-100 hover:bg-stone-700"
                >
                  <a href={agentLiveUrl} target="_blank" rel="noreferrer">
                    Open in another tab
                  </a>
                </Button>
                <Button
                  type="button"
                  onClick={handleCloseLiveFullscreen}
                  className="bg-stone-100 text-stone-900 hover:bg-stone-200"
                >
                  <X className="mr-2 h-4 w-4" /> Close
                </Button>
              </div>
            </div>
            <div className="flex-1 bg-stone-950">
              <iframe
                key={`${agentLiveUrl}-fullscreen`}
                src={agentLiveUrl}
                title="Browser live session fullscreen"
                className="h-full w-full border-0"
                allow="clipboard-read; clipboard-write; accelerometer; autoplay; camera; microphone"
              />
            </div>
          </div>
        )}
      </div>
    </main>
  );
}
