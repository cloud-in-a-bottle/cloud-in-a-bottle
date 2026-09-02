
- use frozen attrs classes, not dataclasses. use `attr.s(auto_attribs=True, frozen=True).
- don't use dictionaries to hold a collection of variables; use attrs classes instead. in general accessing a value from a dictionary with a constant string key (`my_dict["some_attr"]`) is a code smell - the return type of this is most likely not resolved. dictionaries should only be used when actually holding a real variable-size collection of key/value pairs where the values all have the same data type (and the dict should be typed as such).
- prefer to write code that fails loudly, vs continuing in a best-effort manner. eg if you expect an env var to be set, fail if it doesn't exist instead of silently just falling back to a default value. the latter is harder to debug and causes unpleasant surprises.
- don't write file-level docstrings unless the file is particularly confusing or the intent is especially obscure. these just get out of date and confusing. just keep the file simple and readable and concise instead.
- prefer many small code files, well organized in folders, than fewer long files.
- docstrings should line-wrap at the same width as the code, ie 119 chars.
- don't use an Any type or similar lazy typing unless there's truly no better way.
- `__init__.py` files should be empty, except in exceptional situations. laziness is not an exceptional situation.


- .md files should be soft-wrapped, not hard-wrapped.
