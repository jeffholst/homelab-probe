import { useState } from "react";

import type { DraftChange } from "../api/setup";
import { useServices } from "../app/services";
import { SecretInput } from "../components/ui/SecretInput";
import { ErrorBanner, FieldError, StepFrame, messageOf, settingOf, useSetup, useSetupMutation } from "./common";

/**
 * The controller's address and the API key. The key is sent once and kept on the server; the field is emptied as soon
 * as the server has it, and the status only ever says that one is set.
 */
export function ControllerStep({ onNext, onBack }: { onNext: () => void; onBack: () => void }) {
  const { token, status, setStatus } = useSetup();
  const { setup } = useServices();
  const [url, setUrl] = useState(status.draft.url);
  const [apiKey, setApiKey] = useState("");
  const keySet = status.draft.api_key_set;
  const save = useSetupMutation({
    mutationFn: (change: DraftChange) => setup.draft(token, change),
    onSuccess: (next) => {
      setApiKey("");
      setStatus(next);
      onNext();
    },
  });

  const setting = settingOf(save.error);
  const urlError = setting === "UNIFI_URL" ? messageOf(save.error) : null;
  const keyError = setting === "UNIFI_API_KEY" ? messageOf(save.error) : null;
  const [missing, setMissing] = useState<"url" | "key" | null>(null);

  function submit() {
    if (url.trim() === "") {
      setMissing("url");
      return;
    }
    if (apiKey.trim() === "" && !keySet) {
      setMissing("key");
      return;
    }
    setMissing(null);
    save.mutate({ url: url.trim(), ...(apiKey.trim() === "" ? {} : { api_key: apiKey.trim() }) });
  }

  return (
    <StepFrame
      title="Your UniFi controller"
      icon="server"
      lede="Where the controller is, and the API key this server reads it with. Nothing is read from it until you test the connection."
      onSubmit={submit}
      onBack={onBack}
      primary={{ label: "Continue", busy: save.isPending }}
    >
      {save.isError && urlError === null && keyError === null && <ErrorBanner error={save.error} title="Could not save" />}
      <div className="field">
        <label htmlFor="setup-url">Controller address</label>
        <p id="setup-url-hint" className="hint">
          Its <code>https://</code> address on your network, such as <code>https://192.168.1.1</code>.
        </p>
        <input
          id="setup-url"
          className="input"
          type="url"
          inputMode="url"
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          placeholder="https://192.168.1.1"
          value={url}
          aria-invalid={urlError !== null || missing === "url"}
          aria-describedby={`setup-url-hint${urlError !== null || missing === "url" ? " setup-url-error" : ""}`}
          onChange={(event) => {
            setUrl(event.target.value);
          }}
        />
        <FieldError id="setup-url-error" message={urlError ?? (missing === "url" ? "Enter the controller's address." : null)} />
      </div>
      <div className="field">
        <label htmlFor="setup-key">API key</label>
        <p id="setup-key-hint" className="hint">
          {keySet
            ? "A key is saved on the server already. Leave this empty to keep it, or paste a new one."
            : "Create one in UniFi Network under Settings, Control Plane, Integrations. It is kept on this server and never shown again."}
        </p>
        <SecretInput
          id="setup-key"
          mono
          autoComplete="off"
          value={apiKey}
          aria-invalid={keyError !== null || missing === "key"}
          aria-describedby={`setup-key-hint${keyError !== null || missing === "key" ? " setup-key-error" : ""}`}
          onChange={(event) => {
            setApiKey(event.target.value);
          }}
        />
        <FieldError id="setup-key-error" message={keyError ?? (missing === "key" ? "Paste the API key." : null)} />
      </div>
    </StepFrame>
  );
}
