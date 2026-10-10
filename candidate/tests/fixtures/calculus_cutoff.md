# Calculus in plain English

Calculus is the mathematics of **change** and **accumulation**.

It helps answer questions like:

- How fast is something changing right now?
- How far has something traveled?
- What is the exact area under a curve?
- How can we find the highest or lowest possible value?

Calculus has two main parts:

1. **Derivatives** — measure instant change.
2. **Integrals** — measure accumulated total.

A useful way to remember them is:

> Derivatives break change into tiny pieces. Integrals add tiny pieces together.

---

## 1. Functions: the starting point

A **function** is a rule that turns an input into an output.

For example:

\[
f(x)=x^2
\]

This means:

- If \(x=2\), then \(f(2)=4\).
- If \(x=3\), then \(f(3)=9\).

You can imagine a function as a machine:

\[
\text{input } x \longrightarrow \text{function} \longrightarrow \text{output } f(x)
\]

The graph of \(f(x)=x^2\) is a curved U-shape called a **parabola**.

---

# Part I: Derivatives

## 2. What is a derivative?

A derivative tells you the **instantaneous rate of change**.

Its average speed from \(t=1\) to \(t=3\) is:

\[
\frac{9-1}{3-1}=\frac{8}{2}=4
\]

## 5. Basic derivative rules

### Power rule

For

\[
f(x)=x^n
\]

the derivative is:

\[
f'(x)=nx^{n-1}
\]

## 6. Derivatives and real-world meanings

| Function represents | Its derivative represents |
|---|---|
| Position | Velocity |
| Velocity | Acceleration |
| Cost | Marginal cost |

---

# Part II: Integrals

## 11. Definite integrals

\[
\int_a^b f(x)\,dx=F(b)-F(a)
\]

---

# Part III: Limits

## 14. Why limits are needed for derivatives

\[
f'(x)=\lim_{h\to 0}\frac{f(x+h)-f(x)}{h}
\]

Factor and cancel \(h\):

\[
f'(x)=\lim_{h\to 0}(2x+h)
\]

As \(h\) approaches zero:

\[
f'(x)=2x
\]

That is the power rule result.

---

# Part IV: A practical example

## 15. Finding when profit is greatest

Suppose a company’s profit is:

\[
P(x)=-2x^2+40x-100
\]

---

# Part V: The most important ideas

## 16. A simple mental picture

In calculus language:

\[
\text{position} \xrightarrow{\text{derivative}} \text{velocity} \xrightarrow{\text{derivative}} \text{acceleration}
\]

And in reverse:

\[ \text{acceleration} \xrightarrow{\text{integral}} \
