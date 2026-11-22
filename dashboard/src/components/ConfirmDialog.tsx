import { useEffect, useId, useRef } from "react";
import { AlertTriangle } from "lucide-react";
import { useDialogLightDismiss } from "./ModelsTab";

export interface ConfirmState {
  title: string;
  message: string;
  confirmLabel?: string;
  danger?: boolean;
  onConfirm: () => void | Promise<void>;
}

/**
 * Accessible native modal confirmation dialog.
 * - Uses native <dialog> + showModal() (no role="dialog" or aria-modal)
 * - Places autoFocus on the safe Cancel action per destructive-dialog guidance
 * - Reads dialog.returnValue in close listener and immediately clears it
 */
export function ConfirmDialog({
  pending,
  onClose
}: {
  pending: ConfirmState | null;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const pendingRef = useRef<ConfirmState | null>(pending);
  const onCloseRef = useRef(onClose);
  const titleId = useId();
  const descId = useId();

  pendingRef.current = pending;
  onCloseRef.current = onClose;
  useDialogLightDismiss(dialogRef);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;

    const handleClose = () => {
      const choice = dialog.returnValue;
      dialog.returnValue = "";
      const current = pendingRef.current;
      onCloseRef.current();
      if (choice === "confirm" && current) {
        void current.onConfirm();
      }
    };

    dialog.addEventListener("close", handleClose);
    return () => dialog.removeEventListener("close", handleClose);
  }, []);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (pending && !dialog.open) {
      dialog.returnValue = "";
      dialog.showModal();
    } else if (!pending && dialog.open) {
      dialog.close("");
    }
  }, [pending]);

  return (
    <dialog
      ref={dialogRef}
      className="confirm-dialog"
      aria-labelledby={titleId}
      aria-describedby={descId}
      {...({ closedby: "any" } as Record<string, string>)}
    >
      <div className="confirm-dialog__head">
        <AlertTriangle size={18} aria-hidden="true" className="confirm-dialog__icon" />
        <h2 id={titleId}>{pending?.title ?? "Confirm action"}</h2>
      </div>
      <p id={descId} className="confirm-dialog__msg">
        {pending?.message ?? ""}
      </p>
      <form method="dialog" className="modal-actions">
        <button type="submit" value="cancel" className="btn" autoFocus>
          Cancel
        </button>
        <button
          type="submit"
          value="confirm"
          className={`btn ${pending?.danger !== false ? "btn--danger-solid" : "btn--accent"}`}
        >
          {pending?.confirmLabel ?? "Delete"}
        </button>
      </form>
    </dialog>
  );
}
