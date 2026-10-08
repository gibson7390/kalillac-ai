import { Link } from 'wouter';

export default function NotFound() {
  return (
    <main className="not-found">
      <h1>Page not found</h1>
      <p>This page does not exist.</p>
      <Link href="/" className="text-link">Go to the Kalillac homepage</Link>
    </main>
  );
}
