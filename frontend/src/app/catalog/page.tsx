import { redirect } from "next/navigation";

// Daily Drops absorbed the catalog: one page, search or browse. Old links
// (and their ?q= deep links) land in the right place.
export default async function CatalogRedirect({
  searchParams,
}: {
  searchParams: Promise<{ q?: string }>;
}) {
  const { q } = await searchParams;
  redirect(q ? `/dashboard?q=${encodeURIComponent(q)}` : "/dashboard");
}
