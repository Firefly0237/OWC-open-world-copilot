import type { Provenance } from "./provenance";

export interface ExtractionIntakeState<TDraft> {
  draft: TDraft | null;
  intakeToken: string;
  acceptedProvenance: Provenance | null;
  ready: boolean;
}

interface BindExtractionIntakeOptions<TDraft> {
  draft: TDraft;
  intakeToken: string;
  /** Provenance echoed by the server. Missing is tolerated for older job responses only. */
  responseProvenance?: unknown;
  /** Immutable snapshot captured when the run request was created. */
  requestedProvenance: Provenance;
  /** The declaration currently shown by the UI. */
  selectedProvenance: Provenance;
}

const CANON_PROVENANCES: readonly Provenance[] = ["own_ip", "licensed"];

function isCanonProvenance(value: unknown): value is Provenance {
  return CANON_PROVENANCES.some((provenance) => provenance === value);
}

export function emptyExtractionIntakeState<TDraft>(): ExtractionIntakeState<TDraft> {
  return {
    draft: null,
    intakeToken: "",
    acceptedProvenance: null,
    ready: false,
  };
}

export function bindExtractionIntake<TDraft>(
  options: BindExtractionIntakeOptions<TDraft>,
): ExtractionIntakeState<TDraft> {
  // The response is authoritative when present. The request snapshot is only a compatibility
  // fallback; never read a possibly changed reactive selector at asynchronous completion time.
  const accepted =
    options.responseProvenance === undefined
      ? options.requestedProvenance
      : options.responseProvenance;
  if (
    !isCanonProvenance(accepted) ||
    accepted !== options.requestedProvenance ||
    accepted !== options.selectedProvenance ||
    !options.intakeToken.trim()
  ) {
    return emptyExtractionIntakeState();
  }
  return {
    draft: options.draft,
    intakeToken: options.intakeToken,
    acceptedProvenance: accepted,
    ready: true,
  };
}

export function invalidateExtractionIntake<TDraft>(): ExtractionIntakeState<TDraft> {
  return emptyExtractionIntakeState();
}

export function extractionIntakeReady<TDraft>(
  state: ExtractionIntakeState<TDraft> | null,
  selectedProvenance: Provenance,
): state is ExtractionIntakeState<TDraft> & { draft: TDraft; acceptedProvenance: Provenance } {
  return Boolean(
    state?.ready &&
      state.draft &&
      state.intakeToken.trim() &&
      state.acceptedProvenance === selectedProvenance,
  );
}
