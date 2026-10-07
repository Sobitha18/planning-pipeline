(function_declaration
  name: (identifier) @function.name) @function.def

(generator_function_declaration
  name: (identifier) @function.name) @function.def

(class_declaration
  name: (type_identifier) @class.name) @class.def

(method_definition
  name: (property_identifier) @method.name) @method.def

(interface_declaration
  name: (type_identifier) @interface.name) @interface.def

(type_alias_declaration
  name: (type_identifier) @type.name) @type.def

(enum_declaration
  name: (identifier) @enum.name) @enum.def

; const Foo = () => ... / const foo = function() {} -> function (React components)
(variable_declarator
  name: (identifier) @function.name
  value: [(arrow_function) (function_expression)]) @function.def

; const x = someCall(...) -> const (zod schemas, cva variants, etc.)
(variable_declarator
  name: (identifier) @const.name
  value: (call_expression)) @const.def
