import type { Metadata } from "next";
import { PageShell } from "@/components/PageShell";
import { IntakeCascade } from "@/components/intake/IntakeCascade";
import { site } from "@/lib/content";

export const metadata: Metadata = {
  title: `Intake | ${site.name}`,
  description: "Guided new-client intake: card on file, personal info, consents.",
};

export default function IntakePage() {
  return (
    <PageShell crumbs={[{ href: "/", label: "Home" }, { label: "Intake" }]} title="Your intake">
      <IntakeCascade />
    </PageShell>
  );
}
