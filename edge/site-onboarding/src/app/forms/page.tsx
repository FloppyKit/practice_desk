import { redirect } from "next/navigation";

/** The per-form hub is replaced by the guided cascade (/intake). Individual /forms/* pages still work. */
export default async function FormsPage({ searchParams }: { searchParams: Promise<{ link?: string }> }) {
  await searchParams;
  redirect("/intake");
}
